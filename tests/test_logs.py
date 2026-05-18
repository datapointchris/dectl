from dectl.logs import GLUE_ERROR_LOG_GROUP
from dectl.logs import GLUE_OUTPUT_LOG_GROUP


def test_glue_log_groups_are_correct():
    assert GLUE_OUTPUT_LOG_GROUP == '/aws-glue/python-jobs/output'
    assert GLUE_ERROR_LOG_GROUP == '/aws-glue/python-jobs/error'
