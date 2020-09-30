#!/usr/bin/env python3
"""Utility script to run Black Duck scans and query Black Duck database."""
#pylint: disable=too-many-lines

import argparse
import functools
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import warnings

from abc import ABCMeta, abstractmethod
from typing import Dict, List, Optional

import urllib3.util.retry as urllib3_retry
import requests
import yaml

from blackduck.HubRestApi import HubInstance

try:
    import requests.packages.urllib3.exceptions as urllib3_exceptions  #pylint: disable=ungrouped-imports
except ImportError:
    # Versions of the requests package prior to 1.2.0 did not vendor the urllib3 package.
    urllib3_exceptions = None

LOGGER = logging.getLogger(__name__)

############################################################################

# Name of project to upload to and query about
BLACKDUCK_PROJECT = "mongodb/mongo"

# Version of project to query about
# Black Duck automatically determines the version based on branch
BLACKDUCK_PROJECT_VERSION = "master"

# Timeout to wait for a Black Duck scan to complete
BLACKDUCK_TIMEOUT_SECS = 600

# Black Duck hub api uses this file to get settings
BLACKDUCK_RESTCONFIG = ".restconfig.json"

# Wiki page where we document more information about Black Duck
BLACKDUCK_WIKI_PAGE = "https://wiki.corp.mongodb.com/display/KERNEL/Black+Duck"

# Black Duck failed report prefix
BLACKDUCK_FAILED_PREFIX = "A Black Duck scan was run and failed"

############################################################################

# Globals
BLACKDUCK_PROJECT_URL = None

############################################################################

# Build Logger constants

BUILD_LOGGER_CREATE_BUILD_ENDPOINT = "/build"
BUILD_LOGGER_APPEND_GLOBAL_LOGS_ENDPOINT = "/build/%(build_id)s"
BUILD_LOGGER_CREATE_TEST_ENDPOINT = "/build/%(build_id)s/test"
BUILD_LOGGER_APPEND_TEST_LOGS_ENDPOINT = "/build/%(build_id)s/test/%(test_id)s"

BUILD_LOGGER_DEFAULT_URL = "https://logkeeper.mongodb.org"
BUILD_LOGGER_TIMEOUT_SECS = 65

LOCAL_REPORTS_DIR = "bd_reports"

############################################################################

THIRD_PARTY_COMPONENTS_FILE = "etc/third_party_components.yml"

############################################################################


def default_if_none(value, default):
    """Set default if value is 'None'."""
    return value if value is not None else default


# Derived from buildscripts/resmokelib/logging/handlers.py
class HTTPHandler(object):
    """A class which sends data to a web server using POST requests."""

    def __init__(self, url_root, username, password, should_retry=False):
        """Initialize the handler with the necessary authentication credentials."""

        self.auth_handler = requests.auth.HTTPBasicAuth(username, password)

        self.session = requests.Session()

        if should_retry:
            retry_status = [500, 502, 503, 504]  # Retry for these statuses.
            retry = urllib3_retry.Retry(
                backoff_factor=0.1,  # Enable backoff starting at 0.1s.
                method_whitelist=False,  # Support all HTTP verbs.
                status_forcelist=retry_status)

            adapter = requests.adapters.HTTPAdapter(max_retries=retry)
            self.session.mount('http://', adapter)
            self.session.mount('https://', adapter)

        self.url_root = url_root

    def make_url(self, endpoint):
        """Generate a url to post to."""
        return "%s/%s/" % (self.url_root.rstrip("/"), endpoint.strip("/"))

    def post(self, endpoint, data=None, headers=None, timeout_secs=BUILD_LOGGER_TIMEOUT_SECS):
        """
        Send a POST request to the specified endpoint with the supplied data.

        Return the response, either as a string or a JSON object based
        on the content type.
        """

        data = default_if_none(data, [])
        data = json.dumps(data)

        headers = default_if_none(headers, {})
        headers["Content-Type"] = "application/json; charset=utf-8"

        url = self.make_url(endpoint)

        LOGGER.info("POSTING to %s", url)

        with warnings.catch_warnings():
            if urllib3_exceptions is not None:
                try:
                    warnings.simplefilter("ignore", urllib3_exceptions.InsecurePlatformWarning)
                except AttributeError:
                    # Versions of urllib3 prior to 1.10.3 didn't define InsecurePlatformWarning.
                    # Versions of requests prior to 2.6.0 didn't have a vendored copy of urllib3
                    # that defined InsecurePlatformWarning.
                    pass

                try:
                    warnings.simplefilter("ignore", urllib3_exceptions.InsecureRequestWarning)
                except AttributeError:
                    # Versions of urllib3 prior to 1.9 didn't define InsecureRequestWarning.
                    # Versions of requests prior to 2.4.0 didn't have a vendored copy of urllib3
                    # that defined InsecureRequestWarning.
                    pass

            response = self.session.post(url, data=data, headers=headers, timeout=timeout_secs,
                                         auth=self.auth_handler, verify=True)

        response.raise_for_status()

        if not response.encoding:
            response.encoding = "utf-8"

        headers = response.headers

        if headers["Content-Type"].startswith("application/json"):
            return response.json()

        return response.text


# Derived from buildscripts/resmokelib/logging/buildlogger.py
class BuildloggerServer(object):
    # pylint: disable=too-many-instance-attributes
    """
    A remote server to which build logs can be sent.

    It is used to retrieve handlers that can then be added to logger
    instances to send the log to the servers.
    """

    def __init__(self, username, password, task_id, builder, build_num, build_phase, url):
        # pylint: disable=too-many-arguments
        """Initialize BuildloggerServer."""
        self.username = username
        self.password = password
        self.builder = builder
        self.build_num = build_num
        self.build_phase = build_phase
        self.url = url
        self.task_id = task_id

        self.handler = HTTPHandler(url_root=self.url, username=self.username,
                                   password=self.password, should_retry=True)

    def new_build_id(self, suffix):
        """Return a new build id for sending global logs to."""
        builder = "%s_%s" % (self.builder, suffix)
        build_num = int(self.build_num)

        response = self.handler.post(
            BUILD_LOGGER_CREATE_BUILD_ENDPOINT, data={
                "builder": builder,
                "buildnum": build_num,
                "task_id": self.task_id,
            })

        return response["id"]

    def new_test_id(self, build_id, test_filename, test_command):
        """Return a new test id for sending test logs to."""
        endpoint = BUILD_LOGGER_CREATE_TEST_ENDPOINT % {"build_id": build_id}

        response = self.handler.post(
            endpoint, data={
                "test_filename": test_filename,
                "command": test_command,
                "phase": self.build_phase,
                "task_id": self.task_id,
            })

        return response["id"]

    def post_new_file(self, build_id, test_name, lines):
        """Post a new file to the build logger server."""
        test_id = self.new_test_id(build_id, test_name, "foo")
        endpoint = BUILD_LOGGER_APPEND_TEST_LOGS_ENDPOINT % {
            "build_id": build_id,
            "test_id": test_id,
        }

        dt = time.time()

        dlines = [(dt, line) for line in lines]

        try:
            self.handler.post(endpoint, data=dlines)
        except requests.HTTPError as err:
            # Handle the "Request Entity Too Large" error, set the max size and retry.
            raise ValueError("Encountered an HTTP error: %s" % (err))
        except requests.RequestException as err:
            raise ValueError("Encountered a network error: %s" % (err))
        except:  # pylint: disable=bare-except
            raise ValueError("Encountered an error.")

        return self.handler.make_url(endpoint)


def _to_dict(items, func):
    dm = {}

    for i in items:
        tuple1 = func(i)
        dm[tuple1[0]] = tuple1[1]

    return dm


def _compute_security_risk(security_risk_profile):
    counts = security_risk_profile["counts"]

    cm = _to_dict(counts, lambda i: (i["countType"], int(i["count"])))

    priorities = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'OK', 'UNKNOWN']

    for priority in priorities:
        if cm[priority] > 0:
            return priority

    return "OK"


@functools.total_ordering
class VersionInfo:
    """Parse and break apart version strings so they can be compared."""

    def __init__(self, ver_str):
        """Parse a version string input a tuple of ints or mark it as a beta release."""
        try:

            self.ver_str = ver_str
            self.production_version = True

            # Abseil has an empty string for one version
            if self.ver_str == "":
                self.production_version = False
                return

            # Special case Intel's Decimal library since it is just too weird
            if ver_str == "v2.0 U1":
                self.ver_array = [2, 0]
                return

            # "git" is an abseil version
            # "unknown_version" comes from this script when components do not have versions
            # icu has cldr, release, snv, milestone, latest
            # zlib has alt and task
            # boost has ubuntu, fc, old-boost, start, ....
            # BlackDuck thinks boost 1.70.0 was released on 2007 which means we have to check hundreds of versions
            bad_keywords = [
                "unknown_version", "rc", "alpha", "beta", "git", "release", "cldr", "svn", "cvs",
                "milestone", "latest", "alt", "task", "ubuntu", "fc", "old-boost", "start", "split",
                "unofficial", "(", "ctp", "before", "review", "develop", "master", "filesystem",
                "geometry", "icl", "intrusive", "old", "optional", "super", "docs", "+b", "-b",
                "b1", ".0a", "system", "html", "interprocess"
            ]
            if [bad for bad in bad_keywords if bad in self.ver_str]:
                self.production_version = False
                return

            # Clean the version information
            # Some versions start with 'v'. Some components have a mix of 'v' and not 'v' prefixed versions so trim the 'v'
            # MongoDB versions start with 'r'
            if ver_str[0] == 'v' or ver_str[0] == 'r':
                self.ver_str = ver_str[1:]

            # Git hashes are not valid versions
            if len(self.ver_str) == 40 and bytes.fromhex(self.ver_str):
                self.production_version = False
                return

            # Clean out Mozilla's suffix
            self.ver_str = self.ver_str.replace("esr", "")

            # Clean out GPerfTool's prefix
            self.ver_str = self.ver_str.replace("gperftools-", "")

            # Clean out Yaml Cpp's prefix
            self.ver_str = self.ver_str.replace("yaml-cpp-", "")

            # Clean out Boosts's prefix
            self.ver_str = self.ver_str.replace("boost-", "")
            self.ver_str = self.ver_str.replace("asio-", "")

            if self.ver_str.endswith('-'):
                self.ver_str = self.ver_str[0:-1]

            # Some versions end with "-\d", change the "-" since it just means a patch release from a debian/rpm package
            # yaml-cpp has this problem where Black Duck sourced the wrong version information
            self.ver_str = self.ver_str.replace("-", ".")

            # Versions are generally a multi-part integer tuple
            self.ver_array = [int(part) for part in self.ver_str.split(".")]

        except:
            LOGGER.error("Failed to parse version '%s', exception", ver_str)
            raise

    def __repr__(self):
        return self.__str__()

    def __str__(self):
        return ".".join([str(val) for val in self.ver_array])

    def __eq__(self, other):
        return (self.production_version, self.ver_array) == (other.production_version,
                                                             other.ver_array)

    def __gt__(self, other):
        if self.production_version != other.production_version:
            return self.production_version

        return self.ver_array > other.ver_array


def _test_version_info():
    VersionInfo("v2.0 U1")
    VersionInfo("60.7.0-esr")
    VersionInfo("v1.1")
    VersionInfo("0.4.2-1")
    VersionInfo("7.0.2")
    VersionInfo("gperftools-2.8")
    VersionInfo("v1.5-rc2")
    VersionInfo("r4.7.0-alpha")
    VersionInfo("r4.2.10")
    VersionInfo("2.0.0.1")
    VersionInfo("7.0.2-2")
    VersionInfo("git")
    VersionInfo("20200225.2")
    VersionInfo('release-68-alpha')
    VersionInfo('cldr/2020-09-22')
    VersionInfo('release-67-rc')
    VersionInfo('66.1~rc')
    VersionInfo('release-66-rc')
    VersionInfo('release-66-preview')
    VersionInfo('65.1')
    VersionInfo('release-65-rc')
    VersionInfo('64.2-rc')
    VersionInfo('release-64-rc2')
    VersionInfo('release-63-rc')
    VersionInfo('last-cvs-commit')
    VersionInfo('last-svn-commit')
    VersionInfo('release-62-rc')
    VersionInfo('cldr-32-beta2')
    VersionInfo('release-60-rc')
    VersionInfo('milestone-60-0-1')
    VersionInfo('release-59-rc')
    VersionInfo('milestone-59-0-1')
    VersionInfo('release-58-2-eclipse-20170118')
    VersionInfo('tools-release-58')
    VersionInfo('icu-latest')
    VersionInfo('icu4j-latest')
    VersionInfo('icu4j-release-58-1')
    VersionInfo('icu4j-release-58-rc')
    VersionInfo('icu-release-58-rc')
    VersionInfo('icu-milestone-58-0-1')
    VersionInfo('icu4j-milestone-58-0-1')

    VersionInfo('yaml-cpp-0.6.3')

    VersionInfo('gb-c8-task188949.100')
    VersionInfo('1.2.8-alt1.M80C.1')
    VersionInfo('1.2.8-alt2')

    assert VersionInfo('7.0.2.2') > VersionInfo('7.0.0.1')
    assert VersionInfo('7.0.2.2') > VersionInfo('7.0.2')
    assert VersionInfo('7.0.2.2') > VersionInfo('3.1')
    assert VersionInfo('7.0.2.2') <= VersionInfo('8.0.2')


class Component:
    """
    Black Duck Component description.

    Contains a subset of information about a component extracted from Black Duck for a given project and version
    """

    def __init__(self, name, version, licenses, policy_status, security_risk, newest_release):
        # pylint: disable=too-many-arguments
        """Initialize Black Duck component."""
        self.name = name
        self.version = version
        self.licenses = licenses
        self.policy_status = policy_status
        self.security_risk = security_risk
        self.newest_release = newest_release

    @staticmethod
    def parse(hub, component):
        """Parse a Black Duck component from a dictionary."""
        name = component["componentName"]
        cversion = component.get("componentVersionName", "unknown_version")
        licenses = ",".join([a.get("spdxId", a["licenseDisplay"]) for a in component["licenses"]])

        policy_status = component["policyStatus"]
        security_risk = _compute_security_risk(component['securityRiskProfile'])

        newer_releases = component["activityData"].get("newerReleases", 0)

        LOGGER.info("Retrievinng version information for Comp %s - %s  Releases %s", name, cversion,
                    newer_releases)
        cver = VersionInfo(cversion)
        newest_release = None

        # Blackduck's newerReleases is based on "releasedOn" date. This means that if a upstream component releases a beta or rc,
        # it counts as newer but we do not consider those newer for our purposes
        # Missing newerReleases means we do not have to upgrade
        # TODO - remove skip of FireFox since it has soooo many versions
        #if newer_releases > 0 and name not in ("Mozilla Firefox", "Boost C++ Libraries - boost"):

        if newer_releases > 0:
            limit = newer_releases + 1
            versions_url = component["component"] + f"/versions?sort=releasedon%20desc&limit={limit}"

            LOGGER.info("Retrieving version information via %s", versions_url)
            vjson = hub.execute_get(versions_url).json()

            versions = [(ver["versionName"], ver["releasedOn"]) for ver in vjson["items"]]

            LOGGER.info("Known versions: %s ", versions)

            versions = [ver["versionName"] for ver in vjson["items"]]

            # For Firefox, only examine Extended Service Releases (i.e. esr), their long term support releases
            if name == "Mozilla Firefox":
                versions = [ver for ver in versions if "esr" in ver]

            ver_info = [VersionInfo(ver) for ver in versions]
            ver_info = [ver for ver in ver_info if ver.production_version]
            LOGGER.info("Filtered versions: %s ", ver_info)

            ver_info = sorted([ver for ver in ver_info if ver.production_version and ver > cver],
                              reverse=True)

            LOGGER.info("Sorted versions: %s ", ver_info)

            if ver_info:
                newest_release = ver_info[0]

        return Component(name, cversion, licenses, policy_status, security_risk, newest_release)


class BlackDuckConfig:
    """
    Black Duck configuration settings.

    Format is defined by Black Duck Python hub API.
    """

    def __init__(self):
        """Init Black Duck config from disk."""
        if not os.path.exists(BLACKDUCK_RESTCONFIG):
            raise ValueError("Cannot find %s for blackduck configuration" % (BLACKDUCK_RESTCONFIG))

        with open(BLACKDUCK_RESTCONFIG, "r") as rfh:
            rc = json.loads(rfh.read())

        self.url = rc["baseurl"]
        self.username = rc["username"]
        self.password = rc["password"]


def _run_scan():
    # Get user name and password from .restconfig.json
    bdc = BlackDuckConfig()

    with tempfile.NamedTemporaryFile() as fp:
        fp.write(f"""#/!bin/sh
curl --retry 5 -s -L https://detect.synopsys.com/detect.sh  | bash -s -- --blackduck.url={bdc.url} --blackduck.username={bdc.username} --blackduck.password={bdc.password} --detect.report.timeout={BLACKDUCK_TIMEOUT_SECS} --snippet-matching --upload-source --detect.wait.for.results=true
""".encode())
        fp.flush()

        subprocess.call(["/bin/sh", fp.name])


def _scan_cmd_args(args):
    # pylint: disable=unused-argument
    LOGGER.info("Running Black Duck Scan")

    _run_scan()


def _query_blackduck():
    # pylint: disable=global-statement
    global BLACKDUCK_PROJECT_URL

    hub = HubInstance()

    LOGGER.info("Fetching project %s from blackduck", BLACKDUCK_PROJECT)
    project = hub.get_project_by_name(BLACKDUCK_PROJECT)

    LOGGER.info("Fetching project version %s from blackduck", BLACKDUCK_PROJECT_VERSION)
    version = hub.get_version_by_name(project, BLACKDUCK_PROJECT_VERSION)

    LOGGER.info("Getting version components from blackduck")
    bom_components = hub.get_version_components(version)

    components = [Component.parse(hub, comp) for comp in bom_components["items"]]

    BLACKDUCK_PROJECT_URL = version["_meta"]["href"]

    return components


class TestResultEncoder(json.JSONEncoder):
    """JSONEncoder for TestResults."""

    def default(self, o):
        """Serialize objects by default as a dictionary."""
        # pylint: disable=method-hidden
        return o.__dict__


class TestResult:
    """A single test result in the Evergreen report.json format."""

    def __init__(self, name, status, url):
        """Init test result."""
        # This matches the report.json schema
        # See https://github.com/evergreen-ci/evergreen/blob/789bee107d3ffb9f0f82ae344d72502945bdc914/model/task/task.go#L264-L284
        assert status in ["pass", "fail"]

        self.test_file = name
        self.status = status
        self.exit_code = 1

        if url:
            self.url = url
            self.url_raw = url + "?raw=1"

        if status == "pass":
            self.exit_code = 0


class TestResults:
    """Evergreen TestResult format for report.json."""

    def __init__(self):
        """Init test results."""
        self.results = []

    def add_result(self, result: TestResult):
        """Add a test result."""
        self.results.append(result)

    def write(self, filename: str):
        """Write the test results to disk."""

        with open(filename, "w") as wfh:
            wfh.write(json.dumps(self, cls=TestResultEncoder))


class ReportLogger(object, metaclass=ABCMeta):
    """Base Class for all report loggers."""

    @abstractmethod
    def log_report(self, name: str, content: str) -> Optional[str]:
        """Get the command to run a linter."""
        pass


class LocalReportLogger(ReportLogger):
    """Write reports to local directory as a set of files."""

    def __init__(self):
        """Init logger and create directory."""
        if not os.path.exists(LOCAL_REPORTS_DIR):
            os.mkdir(LOCAL_REPORTS_DIR)

    def log_report(self, name: str, content: str) -> Optional[str]:
        """Log report to a local file."""
        file_name = os.path.join(LOCAL_REPORTS_DIR, name + ".log")

        with open(file_name, "w") as wfh:
            wfh.write(content)


class BuildLoggerReportLogger(ReportLogger):
    """Write reports to a build logger server."""

    def __init__(self, build_logger):
        """Init logger."""
        self.build_logger = build_logger

        self.build_id = self.build_logger.new_build_id("bdh")

    def log_report(self, name: str, content: str) -> Optional[str]:
        """Log report to a build logger."""

        content = content.split("\n")

        return self.build_logger.post_new_file(self.build_id, name, content)


def _get_default(list1, idx, default):
    if (idx + 1) < len(list1):
        return list1[idx]

    return default


class TableWriter:
    """Generate an ASCII table that summarizes the results of all the reports generated."""

    def __init__(self, headers: [str]):
        """Init writer."""
        self._headers = headers
        self._rows = []

    def add_row(self, row: [str]):
        """Add a row to the table."""
        self._rows.append(row)

    @staticmethod
    def _write_row(col_sizes: [int], row: [str], writer: io.StringIO):
        writer.write("|")
        for idx, row_value in enumerate(row):
            writer.write(" ")
            writer.write(row_value)
            writer.write(" " * (col_sizes[idx] - len(row_value)))
            writer.write(" |")
        writer.write("\n")

    def print(self, writer: io.StringIO):
        """Print the final table to the string stream."""
        cols = max([len(r) for r in self._rows])

        assert cols == len(self._headers)

        col_sizes = []
        for col in range(0, cols):
            col_sizes.append(
                max([len(_get_default(row, col, []))
                     for row in self._rows] + [len(self._headers[col])]))

        TableWriter._write_row(col_sizes, self._headers, writer)

        TableWriter._write_row(col_sizes, ["-" * c for c in col_sizes], writer)

        for row in self._rows:
            TableWriter._write_row(col_sizes, row, writer)


class TableData:
    """Store scalar values in a two-dimensional matrix indexed by the first column's value."""

    def __init__(self):
        """Init table data."""
        self._rows = {}

    def add_value(self, col: str, value: str):
        """Add a value for a given column. Order sensitive."""
        if col not in self._rows:
            self._rows[col] = []

        self._rows[col].append(value)

    def write(self, headers: [str], writer: io.StringIO):
        """Write table data as nice prettty table to writer."""
        tw = TableWriter(headers)

        for row in self._rows:
            tw.add_row([row] + self._rows[row])

        tw.print(writer)


class ReportManager:
    """Manage logging reports to ReportLogger and generate summary report."""

    def __init__(self, logger: ReportLogger):
        """Init report manager."""
        self._logger = logger
        self._results = TestResults()
        self._data = TableData()

    @staticmethod
    def _get_norm_comp_name(comp_name: str):
        return comp_name.replace(" ", "_").replace("/", "_").lower()

    def write_report(self, comp_name: str, report_name: str, status: str, content: str):
        """
        Write a report about a test to the build logger.

        status is a string of "pass" or "fail"
        """
        comp_name = ReportManager._get_norm_comp_name(comp_name)

        name = comp_name + "_" + report_name

        LOGGER.info("Writing Report %s - %s", name, status)

        self._data.add_value(comp_name, status)

        url = self._logger.log_report(name, content)

        self._results.add_result(TestResult(name, status, url))

    def add_report_metric(self, comp_name: str, metric: str):
        """Add a column to be included in the pretty table."""
        comp_name = ReportManager._get_norm_comp_name(comp_name)

        self._data.add_value(comp_name, metric)

    def finish(self, reports_file: Optional[str]):
        """Generate final summary of all reports run."""

        if reports_file:
            self._results.write(reports_file)

        stream = io.StringIO()

        self._data.write(
            ["Component", "Vulnerability", "Upgrade", "Current Version", "Newest Version"], stream)

        print(stream.getvalue())


class ThirdPartyComponent:
    """MongoDB Third Party component from third_party_components.yml."""

    def __init__(self, name, homepage_url, local_path, team_owner):
        """Init class."""
        # Required fields
        self.name = name
        self.homepage_url = homepage_url
        self.local_path = local_path
        self.team_owner = team_owner

        # optional fields
        self.vulnerability_suppression = None
        self.upgrade_suppression = None


def _get_field(name, ymap, field: str):
    if field not in ymap:
        raise ValueError("Missing field %s for component %s" % (field, name))

    return ymap[field]


def _read_third_party_components():
    with open(THIRD_PARTY_COMPONENTS_FILE) as rfh:
        yaml_file = yaml.load(rfh.read())

    third_party = []
    components = yaml_file["components"]
    for comp in components:
        cmap = components[comp]

        tp = ThirdPartyComponent(comp, _get_field(comp, cmap, 'homepage_url'),
                                 _get_field(comp, cmap, 'local_directory_path'),
                                 _get_field(comp, cmap, 'team_owner'))

        tp.vulnerability_suppression = cmap.get("vulnerability_suppression", None)
        tp.upgrade_suppression = cmap.get("upgrade_suppression", None)

        third_party.append(tp)

    return third_party


def _generate_report_upgrade(mgr: ReportManager, comp: Component, mcomp: ThirdPartyComponent,
                             fail: bool):
    if not fail:
        mgr.write_report(comp.name, "upgrade_check", "pass", "Blackduck run passed")
    else:
        mgr.write_report(
            comp.name, "upgrade_check", "fail", f"""{BLACKDUCK_FAILED_PREFIX}

The {comp.name} library at {mcomp.local_path} is out of date. The current version is
"{comp.version}" and the newest version is "{comp.newest_release}" according to Black Duck.

MongoDB policy requires all third-party software to be updated to the latest version on the master
branch.

Next Steps:

Build Baron: A BF ticket should be generated and assigned to "Service Development Platform" with this text.

Developer: To address this build failure, the next steps are as follows:
1. File a SERVER ticket to update the software if one already does not exist.
2. Add a “upgrade_supression” to {THIRD_PARTY_COMPONENTS_FILE} with the SERVER ticket to acknowledge
   this report. Note that you do not need to immediately update the library, just file a ticket.

If you believe the library is already up-to-date but Black Duck has the wrong version, please update
version information for this component at {BLACKDUCK_PROJECT_URL}.

For more information, {BLACKDUCK_WIKI_PAGE}.
""")

    mgr.add_report_metric(comp.name, str(comp.version))
    mgr.add_report_metric(comp.name, str(comp.newest_release))


def _generate_report_vulnerability(mgr: ReportManager, comp: Component, mcomp: ThirdPartyComponent,
                                   fail: bool):
    if not fail:
        mgr.write_report(comp.name, "vulnerability_check", "pass", "Blackduck run passed")
        return

    mgr.write_report(
        comp.name, "vulnerability_check", "fail", f"""{BLACKDUCK_FAILED_PREFIX}

The {comp.name} library at {mcomp.local_path} had HIGH and/or CRITICAL security issues. The current
version in Black Duck is "{comp.version}".

MongoDB policy requires all third-party software to be updated to a version clean of HIGH and
CRITICAL vulnerabilities on the master branch.

Next Steps:

Build Baron: A BF ticket should be generated and assigned to "Service Development Platform" with this text.

Developer: To address this build failure, the next steps are as follows:
1. File a SERVER ticket to update the software if one already does not exist. Note that you do not
   need to immediately update the library, just file a ticket.
2. Add a “vulnerability_supression” to {THIRD_PARTY_COMPONENTS_FILE} with the SERVER ticket to
   acknowledge this report.

If you believe the library is already up-to-date but Black Duck has the wrong version, please update
version information for this component at {BLACKDUCK_PROJECT_URL}.

 For more information, {BLACKDUCK_WIKI_PAGE}.
""")


class Analyzer:
    """
    Analyze the MongoDB source code for software maintence issues.

    Queries Black Duck for out of date software
    Consults a local yaml file for detailed information about third party components included in the MongoDB source code.
    """

    def __init__(self):
        """Init analyzer."""
        self.third_party_components = None
        self.third_party_directories = None
        self.black_duck_components = None
        self.mgr = None

    def _do_reports(self):
        for comp in self.black_duck_components:
            # 1. Validate there are no security issues
            self._verify_vulnerability_status(comp)

            # 2. Check for upgrade issues
            self._verify_upgrade_status(comp)

    def _verify_upgrade_status(self, comp: Component):
        mcomp = self._get_mongo_component(comp)

        if comp.newest_release and not mcomp.upgrade_suppression:
            _generate_report_upgrade(self.mgr, comp, mcomp, True)
        else:
            _generate_report_upgrade(self.mgr, comp, mcomp, False)

    def _verify_vulnerability_status(self, comp: Component):
        mcomp = self._get_mongo_component(comp)

        if comp.security_risk in ["HIGH", "CRITICAL"] and not mcomp.vulnerability_suppression:
            _generate_report_vulnerability(self.mgr, comp, mcomp, True)
        else:
            _generate_report_vulnerability(self.mgr, comp, mcomp, False)

    def _get_mongo_component(self, comp: Component):
        mcomp = next((x for x in self.third_party_components if x.name == comp.name), None)

        if not mcomp:
            raise ValueError(
                "Cannot find third party component for Black Duck Component '%s'. Please update '%s'. "
                % (comp.name, THIRD_PARTY_COMPONENTS_FILE))

        return mcomp

    def run(self, logger: ReportLogger, report_file: Optional[str]):
        """Run analysis of Black Duck scan and local files."""

        self.third_party_components = _read_third_party_components()

        self.black_duck_components = _query_blackduck()

        # Black Duck detects ourself everytime we release a new version
        # Rather then constantly have to supress this in Black Duck itself which will generate false positives
        # We filter ourself our of the list of components.
        self.black_duck_components = [
            comp for comp in self.black_duck_components if not comp.name == "MongoDB"
        ]

        self.mgr = ReportManager(logger)

        self._do_reports()

        self.mgr.finish(report_file)


# Derived from buildscripts/resmokelib/logging/buildlogger.py
def _get_build_logger_from_file(filename, build_logger_url, task_id):
    tmp_globals = {}
    config = {}

    # The build logger config file is actually python
    # It is a mix of quoted strings and ints
    exec(compile(open(filename, "rb").read(), filename, 'exec'), tmp_globals, config)

    # Rename "slavename" to "username" if present.
    if "slavename" in config and "username" not in config:
        config["username"] = config["slavename"]
        del config["slavename"]

    # Rename "passwd" to "password" if present.
    if "passwd" in config and "password" not in config:
        config["password"] = config["passwd"]
        del config["passwd"]

    return BuildloggerServer(config["username"], config["password"], task_id, config["builder"],
                             config["build_num"], config["build_phase"], build_logger_url)


def _generate_reports_args(args):
    LOGGER.info("Generating Reports")

    # Log to LOCAL_REPORTS_DIR directory unless build logger is explicitly chosen
    logger = LocalReportLogger()

    if args.build_logger_local:
        build_logger = BuildloggerServer("fake_user", "fake_pass", "fake_task", "fake_builder", 1,
                                         "fake_build_phase", "http://localhost:8080")
        logger = BuildLoggerReportLogger(build_logger)
    elif args.build_logger:
        if not args.build_logger_task_id:
            raise ValueError("Must set build_logger_task_id if using build logger")

        build_logger = _get_build_logger_from_file(args.build_logger, args.build_logger_url,
                                                   args.build_logger_task_id)
        logger = BuildLoggerReportLogger(build_logger)

    analyzer = Analyzer()
    analyzer.run(logger, args.report_file)


def _scan_and_report_args(args):
    LOGGER.info("Running Black Duck Scan And Generating Reports")

    _run_scan()

    _generate_reports_args(args)


def main() -> None:
    """Execute Main entry point."""

    parser = argparse.ArgumentParser(description='Black Duck hub controller.')

    parser.add_argument('-v', "--verbose", action='store_true', help="Enable verbose logging")
    parser.add_argument('-d', "--debug", action='store_true', help="Enable debug logging")

    sub = parser.add_subparsers(title="Hub subcommands", help="sub-command help")
    generate_reports_cmd = sub.add_parser('generate_reports',
                                          help='Generate reports from Black Duck')

    generate_reports_cmd.add_argument("--report_file", type=str,
                                      help="report json file to write to")
    generate_reports_cmd.add_argument(
        "--build_logger", type=str, help="Log to build logger with credentials from specified file")
    generate_reports_cmd.add_argument("--build_logger_url", type=str,
                                      default=BUILD_LOGGER_DEFAULT_URL,
                                      help="build logger url to log to")
    generate_reports_cmd.add_argument("--build_logger_task_id", type=str,
                                      help="build logger task id")
    generate_reports_cmd.add_argument("--build_logger_local", action='store_true',
                                      help="Log to local build logger, logs to disk by default")
    generate_reports_cmd.set_defaults(func=_generate_reports_args)

    scan_cmd = sub.add_parser('scan', help='Do Black Duck Scan')
    scan_cmd.set_defaults(func=_scan_cmd_args)

    scan_and_report_cmd = sub.add_parser('scan_and_report',
                                         help='Run scan and then generate reports')
    scan_and_report_cmd.add_argument("--report_file", type=str, help="report json file to write to")

    scan_and_report_cmd.add_argument(
        "--build_logger", type=str, help="Log to build logger with credentials from specified file")
    scan_and_report_cmd.add_argument("--build_logger_url", type=str,
                                     default=BUILD_LOGGER_DEFAULT_URL,
                                     help="build logger url to log to")
    scan_and_report_cmd.add_argument("--build_logger_task_id", type=str,
                                     help="build logger task id")
    scan_and_report_cmd.add_argument("--build_logger_local", action='store_true',
                                     help="Log to local build logger, logs to disk by default")
    scan_and_report_cmd.set_defaults(func=_scan_and_report_args)

    args = parser.parse_args()

    _test_version_info()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    elif args.verbose:
        logging.basicConfig(level=logging.INFO)

    args.func(args)


if __name__ == "__main__":
    main()