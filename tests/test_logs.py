from dectl.logs import GLUE_ERROR_LOG_GROUP
from dectl.logs import GLUE_OUTPUT_LOG_GROUP
from dectl.logs import stream_prefix


def test_glue_log_groups_are_correct():
    assert GLUE_OUTPUT_LOG_GROUP == '/aws-glue/python-jobs/output'
    assert GLUE_ERROR_LOG_GROUP == '/aws-glue/python-jobs/error'


def test_stream_prefix_tags_error_group():
    assert stream_prefix(GLUE_ERROR_LOG_GROUP) == '[red]err[/red] '


def test_stream_prefix_tags_output_group():
    assert stream_prefix(GLUE_OUTPUT_LOG_GROUP) == '[cyan]out[/cyan] '
