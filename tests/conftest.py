# Copyright (c) 2012 Cloudera, Inc. All rights reserved.
# py.test configuration module
#
import logging
import os
import pytest

from zlib import crc32

from common.test_result_verifier import QueryTestResult
from tests.common.patterns import is_valid_impala_identifier
from tests.util.filesystem_utils import FILESYSTEM, ISILON_WEBHDFS_PORT


logging.basicConfig(level=logging.INFO, format='%(threadName)s: %(message)s')
LOG = logging.getLogger('test_configuration')


def _get_default_nn_http_addr():
  """Return the namenode ip and webhdfs port if the default shouldn't be used"""
  if FILESYSTEM == 'isilon':
    return "%s:%s" % (os.getenv("ISILON_NAMENODE"), ISILON_WEBHDFS_PORT)
  return None


def pytest_addoption(parser):
  """Adds a new command line options to py.test"""
  parser.addoption("--exploration_strategy", default="core",
                   help="Default exploration strategy for all tests. Valid values: core, "
                   "pairwise, exhaustive.")

  parser.addoption("--workload_exploration_strategy", default=None,
                   help="Override exploration strategy for specific workloads using the "
                   "format: workload:exploration_strategy. Ex: tpch:core,tpcds:pairwise.")

  parser.addoption("--impalad", default="localhost:21000,localhost:21001,localhost:21002",
                   help="A comma-separated list of impalad host:ports to target. Note: "
                   "Not all tests make use of all impalad, some tests target just the "
                   "first item in the list (it is considered the 'default'")

  parser.addoption("--impalad_hs2_port", default="21050",
                   help="The impalad HiveServer2 port.")

  parser.addoption("--metastore_server", default="localhost:9083",
                   help="The Hive Metastore server host:port to connect to.")

  parser.addoption("--hive_server2", default="localhost:11050",
                   help="Hive's HiveServer2 host:port to connect to.")

  default_xml_path = os.path.join(os.environ['HADOOP_CONF_DIR'], "hdfs-site.xml")
  parser.addoption("--minicluster_xml_conf", default=default_xml_path,
                   help="The full path to the HDFS xml configuration file")

  parser.addoption("--namenode_http_address", default=_get_default_nn_http_addr(),
                   help="The host:port for the HDFS Namenode's WebHDFS interface. Takes"
                   " precedence over any configuration read from --minicluster_xml_conf")

  parser.addoption("--update_results", action="store_true", default=False,
                   help="If set, will generate new results for all tests run instead of "
                   "verifying the results.")

  parser.addoption("--table_formats", dest="table_formats", default=None,
                   help="Override the test vectors and run only using the specified "
                   "table formats. Ex. --table_formats=seq/snap/block,text/none")

  parser.addoption("--scale_factor", dest="scale_factor", default=None,
                   help="If running on a cluster, specify the scale factor"
                   "Ex. --scale_factor=500gb")

# KERBEROS TODO: I highly doubt that the default is correct.  Try "hive".
  parser.addoption("--hive_service_name", dest="hive_service_name",
                   default="Hive Metastore Server",
                   help="The principal service name for the hive metastore client when "
                   "using kerberos.")

  parser.addoption("--use_kerberos", action="store_true", default=False,
                   help="use kerberos transport for running tests")

  parser.addoption("--sanity", action="store_true", default=False,
                   help="Runs a single test vector from each test to provide a quick "
                   "sanity check at the cost of lower test coverage.")

  parser.addoption("--skip_hbase", action="store_true", default=False,
                   help="Skip HBase tests")


def pytest_assertrepr_compare(op, left, right):
  """
  Provides a hook for outputting type-specific assertion messages

  Expected to return a list or strings, where each string will be printed as a new line
  in the result output report on assertion failure.
  """
  if isinstance(left, QueryTestResult) and isinstance(right, QueryTestResult) and \
     op == "==":
    result = ['Comparing QueryTestResults (expected vs actual):']
    for l, r in map(None, left.rows, right.rows):
      result.append("%s == %s" % (l, r) if l == r else "%s != %s" % (l, r))
    if len(left.rows) != len(right.rows):
      result.append('Number of rows returned (expected vs actual): '
                    '%d != %d' % (len(left.rows), len(right.rows)))

    # pytest has a bug/limitation where it will truncate long custom assertion messages
    # (it has a hardcoded string length limit of 80*8 characters). To ensure this info
    # isn't lost, always log the assertion message.
    LOG.error('\n'.join(result))
    return result

  # pytest supports printing the diff for a set equality check, but does not do
  # so well when we're doing a subset check. This handles that situation.
  if isinstance(left, set) and isinstance(right, set) and op == '<=':
    # If expected is not a subset of actual, print out the set difference.
    result = ['Items in expected results not found in actual results:']
    result.append(('').join(list(left - right)))
    LOG.error('\n'.join(result))
    return result


def pytest_xdist_setupnodes(config, specs):
  """Hook that is called when setting up the xdist plugin"""
  # Force the xdist plugin to be quiet. In verbose mode it spews useless information.
  config.option.verbose = 0


def pytest_generate_tests(metafunc):
  """
  This is a hook to parameterize the tests based on the input vector.

  If a test has the 'vector' fixture specified, this code is invoked and it will build
  a set of test vectors to parameterize the test with.
  """
  if 'vector' in metafunc.fixturenames:
    metafunc.cls.add_test_dimensions()
    vectors = metafunc.cls.TestMatrix.generate_test_vectors(
        metafunc.config.option.exploration_strategy)
    if len(vectors) == 0:
      LOG.warning('No test vectors generated. Check constraints and input vectors')

    vector_names = map(str, vectors)
    # In the case this is a test result update or sanity run, select a single test vector
    # to run. This is okay for update_results because results are expected to be the same
    # for all test vectors.
    if metafunc.config.option.update_results or metafunc.config.option.sanity:
      vectors = vectors[0:1]
      vector_names = vector_names[0:1]
    metafunc.parametrize('vector', vectors, ids=vector_names)


@pytest.fixture
def testid_checksum(request):
  """
  Return a hex string representing the CRC32 checksum of the parametrized test
  function's full pytest test ID. The full pytest ID includes relative path, module
  name, possible test class name, function name, and any parameters.

  This could be combined with some prefix in order to form identifiers unique to a
  particular test case.
  """
  # For an example of what a full pytest ID looks like, see below (written as Python
  # multi-line literal)
  #
  # ("query_test/test_cancellation.py::TestCancellationParallel::()::test_cancel_select"
  #  "[table_format: avro/snap/block | exec_option: {'disable_codegen': False, "
  #  "'abort_on_error': 1, 'exec_single_node_rows_threshold': 0, 'batch_size': 0, "
  #  "'num_nodes': 0} | query_type: SELECT | cancel_delay: 3 | action: WAIT | "
  #  "query: select l_returnflag from lineitem]")
  return '{0:x}'.format(crc32(request.node.nodeid) & 0xffffffff)


@pytest.fixture
def unique_database(request, testid_checksum):
  """
  Return a database name unique to any test using the fixture. The fixture creates the
  database during setup, allows the test to know the database name, and drops the
  database after the test has completed.

  By default, the database name is a concatenation of the test function name and the
  testid_checksum. The database name prefix can be changed via parameter (see below).

  A good candidate for a test to use this fixture is a test that needs to have a
  test-local database or test-local tables that are created and destroyed at test run
  time. Because the fixture generates a unique name, tests using this fixture can be run
  in parallel as long as they don't need exclusion on other test-local resources.

  Sample usage:

    def test_something(self, vector, unique_database):
      # fixture creates database test_something_48A80F
      self.client.execute('DROP TABLE IF EXISTS `{0}`.`mytable`'.format(unique_database))
      # test does other stuff with the unique_database name as needed

  We also allow for parametrization:

    from tests.common.parametrize import UniqueDatabase

    @UniqueDatabase.parametrize(name_prefix='mydb')
    def test_something(self, vector, unique_database):
      # fixture creates database mydb_48A80F
      self.client.execute('DROP TABLE IF EXISTS `{0}`.`mytable`'.format(unique_database))
      # test does other stuff with the unique_database name as needed

  The supported parameter:

    name_prefix: string (defaults to test function __name__) - prefix to be used for the
    database name
  """

  # Test cases are at the function level, so no one should "accidentally" re-scope this.
  assert 'function' == request.scope, ('This fixture must have scope "function" since '
                                       'the fixture must guarantee unique per-test '
                                       'databases.')

  db_name_prefix = getattr(request, 'param', request.function.__name__)

  db_name = '{0}_{1}'.format(db_name_prefix, testid_checksum)
  if not is_valid_impala_identifier(db_name):
    raise ValueError('Unique database name "{0}" is not a valid Impala identifer; check '
                     'test function name or any prefixes for long length or invalid '
                     'characters.'.format(db_name))

  def cleanup():
    # Make sure we don't try to drop the current session database
    request.instance.execute_query_expect_success(request.instance.client, "use default")
    request.instance.execute_query_expect_success(
        request.instance.client, 'DROP DATABASE `{0}` CASCADE'.format(db_name))
    LOG.info('Dropped database "{0}" for test ID "{1}"'.format(db_name,
                                                               str(request.node.nodeid)))

  request.addfinalizer(cleanup)

  request.instance.execute_query_expect_success(
      request.instance.client, 'DROP DATABASE IF EXISTS `{0}` CASCADE'.format(db_name))
  request.instance.execute_query_expect_success(
      request.instance.client, 'CREATE DATABASE `{0}`'.format(db_name))
  LOG.info('Created database "{0}" for test ID "{1}"'.format(db_name,
                                                             str(request.node.nodeid)))
  return db_name
