"""A state machine definition written as a Terraform template: rendered, and compared with the live one.

Terraform fills a definition file's `${name}` placeholders with `templatefile`, and the AWS
provider compares the live definition string with that rendered text without normalizing the
JSON. So `render_definition` reproduces Terraform's output byte for byte rather than parsing and
re-serializing it. A push that changed only whitespace would otherwise show as a definition
change on every later `terraform plan`.

Only a bare name is substituted. A directive (`%{ if }`), an expression (`${upper(x)}`) or a
strip marker (`${~ x}`) is reported rather than approximated, because an approximation renders a
definition Terraform would not.
"""

import bisect
import difflib
import json
import re
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Any
from typing import NamedTuple

# Alternatives are tried in order at each position, escapes first, which is how Terraform scans:
# `$$${x}` renders as `$${x}` and `%%%{x}` as `%%{x}`.
TEMPLATE_SEQUENCE = re.compile(
    r'(?P<escaped_interpolation>\$\$\{)'
    r'|(?P<escaped_directive>%%\{)'
    r'|\$\{(?P<interpolation>[^}]*)\}'
    r'|%\{(?P<directive>[^}]*)\}'
    r'|(?P<unclosed>\$\{|%\{)'
)
# An HCL identifier, which may carry a dash, with the whitespace `${ name }` allows around it.
BARE_NAME = re.compile(r'\s*([A-Za-z_][A-Za-z0-9_-]*)\s*')

# Private-use code points, so a sentinel cannot collide with anything a definition holds. A
# missing placeholder inside a JSON string is spliced into it as an embedded sentinel. One outside
# a string becomes a string holding only a bare sentinel, so the document still parses.
EMBEDDED_OPEN = ''
EMBEDDED_CLOSE = ''
EMBEDDED = re.compile(f'{EMBEDDED_OPEN}([^{EMBEDDED_CLOSE}]*){EMBEDDED_CLOSE}')
BARE_OPEN = ''
BARE_CLOSE = ''
BARE = re.compile(f'{BARE_OPEN}([^{BARE_CLOSE}]*){BARE_CLOSE}')

ABSENT = object()


class TemplateFault(StrEnum):
    MISSING_SUBSTITUTION = 'missing_substitution'
    NOT_A_BARE_NAME = 'not_a_bare_name'
    DIRECTIVE = 'directive'
    UNCLOSED = 'unclosed'


TEMPLATE_FAULT_WORDING = {
    TemplateFault.MISSING_SUBSTITUTION: 'has no entry in definition_substitutions',
    TemplateFault.NOT_A_BARE_NAME: 'is not a bare name, and dectl substitutes ${name} only',
    TemplateFault.DIRECTIVE: 'is a template directive, which dectl does not render',
    TemplateFault.UNCLOSED: 'opens a placeholder that never closes',
}


class TemplateProblem(NamedTuple):
    fault: TemplateFault
    # As written in the file, so the reader can search for it.
    fragment: str
    line: int
    # The placeholder's name, for a missing substitution only.
    name: str = ''


class Rendering(NamedTuple):
    text: str
    problems: list[TemplateProblem]


class Suggestion(NamedTuple):
    # A number or boolean where the placeholder sits outside a string, so it pastes as one.
    value: str | int | float | bool
    # A JSON pointer, the form ValidateStateMachineDefinition reports locations in.
    location: str


def template_text(value: Any) -> Any:
    """A number or boolean as Terraform writes it into a template, and anything else untouched.

    Terraform writes a number in plain decimal with no trailing zero, so `2.0` renders as `2` and
    `1e-7` as `0.0000001`. Python's `str` writes neither that way."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(Decimal(repr(value)).normalize(), 'f')
    return value


def render_definition(template: str, substitutions: Mapping[str, str]) -> Rendering:
    """The template with every placeholder replaced, and every one that could not be."""
    return render_with(template, lambda name, _offset: substitutions.get(name))


def render_with(template: str, lookup: Callable[[str, int], str | None]) -> Rendering:
    """Render with `lookup`, which is handed each placeholder's name and its offset in the template."""
    problems: list[TemplateProblem] = []

    def replace(match: re.Match[str]) -> str:
        if match['escaped_interpolation']:
            return '${'
        if match['escaped_directive']:
            return '%{'
        fragment = match[0]
        line = template.count('\n', 0, match.start()) + 1
        if match['interpolation'] is not None:
            bare = BARE_NAME.fullmatch(match['interpolation'])
            if bare is None:
                problems.append(TemplateProblem(TemplateFault.NOT_A_BARE_NAME, fragment, line))
                return fragment
            value = lookup(bare[1], match.start())
            if value is None:
                problems.append(TemplateProblem(TemplateFault.MISSING_SUBSTITUTION, fragment, line, bare[1]))
                return fragment
            return value
        fault = TemplateFault.DIRECTIVE if match['directive'] is not None else TemplateFault.UNCLOSED
        problems.append(TemplateProblem(fault, fragment, line))
        return fragment

    return Rendering(TEMPLATE_SEQUENCE.sub(replace, template), problems)


def suggest_substitutions(template: str, substitutions: Mapping[str, str], live_definition: str) -> dict[str, Suggestion]:
    """For each placeholder with no substitution, what the live definition holds where it sits.

    The live state machine was rendered by Terraform from an earlier copy of this file, so the
    value at a placeholder's JSON path is usually the one it needs. Usually, not always: a state
    edited to call a different function holds the old one there. That is why this only feeds a
    refusal the reader acts on, and never fills a value itself.

    Two placeholders with nothing between them are skipped, because matching cannot tell where
    one ends. A placeholder outside a string with anything beside it leaves the document
    unparsable, and then nothing is suggested."""
    strings = string_spans(template)

    def lookup(name: str, offset: int) -> str:
        if name in substitutions:
            return substitutions[name]
        if inside(strings, offset):
            return f'{EMBEDDED_OPEN}{name}{EMBEDDED_CLOSE}'
        return f'"{BARE_OPEN}{name}{BARE_CLOSE}"'

    try:
        rendered = json.loads(render_with(template, lookup).text)
        live = json.loads(live_definition)
    except json.JSONDecodeError:
        return {}

    found: dict[str, Suggestion] = {}
    for path, value in string_leaves(rendered):
        live_value = value_at(live, path)
        if live_value is ABSENT:
            continue
        if bare := BARE.fullmatch(value):
            found.setdefault(bare[1], Suggestion(bare_value(live_value), json_pointer(path)))
            continue
        parts = EMBEDDED.split(value)
        if len(parts) == 1 or not isinstance(live_value, str):
            continue
        literals, names = parts[0::2], parts[1::2]
        if any(not literal for literal in literals[1:-1]):
            continue
        pattern = '(.+?)'.join(re.escape(literal) for literal in literals)
        match = re.fullmatch(pattern, live_value, re.DOTALL)
        if match is None:
            continue
        for name, captured in zip(names, match.groups(), strict=True):
            found.setdefault(name, Suggestion(captured, json_pointer(path)))
    return found


def bare_value(live_value: Any) -> str | int | float | bool:
    """What a placeholder outside a string holds to render `live_value`: a number or boolean as
    itself, and anything else as its JSON text."""
    if isinstance(live_value, bool | int | float):
        return live_value
    return json.dumps(live_value, ensure_ascii=False)


def string_spans(text: str) -> list[tuple[int, int]]:
    """The offsets of each JSON string's opening and closing quote in `text`."""
    spans = []
    start = text.find('"')
    while start != -1:
        end = start + 1
        while end < len(text) and text[end] != '"':
            end += 2 if text[end] == '\\' else 1
        spans.append((start, end))
        start = text.find('"', end + 1)
    return spans


def inside(spans: list[tuple[int, int]], offset: int) -> bool:
    index = bisect.bisect_right(spans, offset, key=lambda span: span[0]) - 1
    return index >= 0 and offset < spans[index][1]


def string_leaves(node: Any, path: tuple = ()) -> Iterator[tuple[tuple, str]]:
    if isinstance(node, dict):
        for key, child in node.items():
            yield from string_leaves(child, (*path, key))
    elif isinstance(node, list):
        for index, child in enumerate(node):
            yield from string_leaves(child, (*path, index))
    elif isinstance(node, str):
        yield path, node


def value_at(node: Any, path: tuple) -> Any:
    """The value at `path`, or `ABSENT`, since a JSON null is a value a path can hold."""
    for step in path:
        if not has_child(node, step):
            return ABSENT
        node = node[step]
    return node


def has_child(node: Any, step: Any) -> bool:
    if isinstance(node, dict):
        return isinstance(step, str) and step in node
    return isinstance(node, list) and isinstance(step, int) and step < len(node)


def json_pointer(path: tuple) -> str:
    # RFC 6901 escapes `~` before `/`, so an escaped slash is not read back as a tilde.
    return ''.join('/' + str(step).replace('~', '~0').replace('/', '~1') for step in path)


def definition_diff(live: Any, rendered: Any, live_label: str, rendered_label: str) -> list[str]:
    """A unified diff of two parsed definitions, both pretty-printed in their own key order."""
    return list(difflib.unified_diff(pretty_lines(live), pretty_lines(rendered), fromfile=live_label, tofile=rendered_label, lineterm=''))


def pretty_lines(definition: Any) -> list[str]:
    return json.dumps(definition, indent=2, ensure_ascii=False).splitlines()
