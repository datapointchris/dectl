"""An HTTP endpoint standing in for AWS and Jenkins, recording every request it is sent.

dectl and the aws CLI both honor AWS_ENDPOINT_URL, so pointed at one of these they send it the
requests they would send AWS. Each request is decoded with botocore's own service models into the
operation and the parameters its caller passed. A test then compares what two callers asked for,
never how each spelled it: a JSON body member, a query string and a header all arrive as one
parameter.

A request carrying no SigV4 signature is a Jenkins one, recorded by method and path."""

import base64
import functools
import json
import re
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from typing import Any
from typing import NamedTuple

import botocore.session
from botocore.model import OperationModel
from botocore.model import ServiceModel
from botocore.model import StructureShape

CREDENTIAL_SCOPE = re.compile(r'Credential=(?P<access_key>[^/]+)/\d{8}/[^/]+/(?P<signing_name>[^/]+)/aws4_request')

# The scope carries the signing name, which is not the service name for Step Functions.
SERVICE_BY_SIGNING_NAME = {'states': 'stepfunctions'}

# Pinned in S3's request URIs to name the operation for logging, and sent by some callers only.
UNPINNED_QUERY_KEYS = frozenset({'x-id'})

JSON_CONTENT_TYPES = {'json': 'application/x-amz-json-{version}', 'rest-json': 'application/json'}


class Call(NamedTuple):
    """One request: who sent it, and the operation and parameters it asked for.

    The caller is the access key a request was signed with, or the Jenkins user, so processes
    given different credentials share one endpoint and their calls stay apart."""

    caller: str
    service: str
    operation: str
    params: dict[str, Any]


@dataclass(frozen=True)
class Reply:
    status: int = 200
    body: dict | list | bytes | str | None = None
    headers: dict[str, str] = field(default_factory=dict)


def error_reply(code: str, message: str, status: int = 400) -> Reply:
    return Reply(status, {'__type': code, 'message': message}, {'x-amzn-ErrorType': code})


class RestRoute(NamedTuple):
    """How a request reaches one operation of a REST service.

    Method and path are not enough: S3's PutObject and CopyObject are both `PUT /{Bucket}/{Key+}`,
    and only CopyObject's required `x-amz-copy-source` header tells them apart. So a route also
    requires the query keys its URI pins and the headers and query members its input requires."""

    operation: OperationModel
    method: str
    path: re.Pattern[str]
    labels: dict[str, str]
    required_query: frozenset[str]
    required_headers: frozenset[str]
    specificity: tuple[int, int]


@functools.cache
def service_model(service: str) -> ServiceModel:
    return botocore.session.get_session().get_service_model(service)


@functools.cache
def rest_routes(service: str) -> list[RestRoute]:
    """Every operation of a REST service as the method and path pattern that reach it."""
    model = service_model(service)
    routes = []
    for name in model.operation_names:
        operation = model.operation_model(name)
        path, _, pinned = operation.http['requestUri'].partition('?')
        pattern, literal = '', 0
        labels: dict[str, str] = {}
        for piece in re.split(r'(\{[^}]+\})', path):
            if piece.startswith('{'):
                label = piece[1:-1]
                group = f'label{len(labels)}'
                labels[group] = label.rstrip('+')
                pattern += f'(?P<{group}>{".+" if label.endswith("+") else "[^/]+"})'
            else:
                pattern += re.escape(piece)
                literal += len(piece)
        query = {key for key, _ in urllib.parse.parse_qsl(pinned, keep_blank_values=True)} - UNPINNED_QUERY_KEYS
        headers = set()
        shape = operation.input_shape
        for name in shape.required_members if shape is not None else []:
            serialization = shape.members[name].serialization
            if serialization.get('location') == 'header':
                headers.add(serialization.get('name', name).lower())
            elif serialization.get('location') == 'querystring':
                query.add(serialization.get('name', name))
        specificity = (len(query) + len(headers), literal)
        routes.append(
            RestRoute(operation, operation.http['method'], re.compile(pattern), labels, frozenset(query), frozenset(headers), specificity)
        )
    return routes


def decode_blobs(shape: StructureShape, document: dict) -> dict:
    """A JSON body's members, with each blob member base64-decoded as botocore encoded it."""
    return {
        name: base64.b64decode(value) if name in shape.members and shape.members[name].type_name == 'blob' else value
        for name, value in document.items()
    }


def rest_operation(
    service: str, method: str, url: urllib.parse.SplitResult, headers: dict[str, str], body: bytes
) -> tuple[str, dict[str, Any]]:
    """The operation a REST request reaches, and its parameters gathered from wherever they travelled."""
    query = urllib.parse.parse_qs(url.query, keep_blank_values=True)
    lowered = {key.lower(): value for key, value in headers.items()}
    candidates = []
    for route in rest_routes(service):
        match = route.path.fullmatch(url.path)
        if route.method == method and match and route.required_query <= query.keys() and route.required_headers <= lowered.keys():
            candidates.append((route.specificity, route, match))
    if not candidates:
        return f'{method} {url.path}', {'query': query}
    _, route, match = max(candidates, key=lambda candidate: candidate[0])
    shape = route.operation.input_shape
    params: dict[str, Any] = {}
    if shape is None:
        return route.operation.name, params
    labels = {route.labels[group]: urllib.parse.unquote(value) for group, value in match.groupdict().items()}
    for name, member in shape.members.items():
        location = member.serialization.get('location')
        wire = member.serialization.get('name', name)
        if location == 'uri' and wire in labels:
            params[name] = labels[wire]
        elif location == 'querystring' and wire in query:
            params[name] = query[wire] if member.type_name == 'list' else query[wire][0]
        elif location == 'header' and wire.lower() in lowered:
            params[name] = lowered[wire.lower()]
        elif location == 'headers':
            prefixed = {key[len(wire) :]: value for key, value in lowered.items() if key.startswith(wire.lower())}
            if prefixed:
                params[name] = prefixed
    payload = shape.serialization.get('payload')
    if payload:
        if body:
            params[payload] = body
    elif body and service_model(service).protocol == 'rest-json':
        params |= decode_blobs(shape, json.loads(body))
    return route.operation.name, params


def decode_chunks(body: bytes) -> bytes:
    """An `aws-chunked` or HTTP chunked body as the bytes it carries, its signatures and trailers dropped."""
    decoded, position = b'', 0
    while True:
        line_end = body.index(b'\r\n', position)
        size = int(body[position:line_end].split(b';')[0], 16)
        if size == 0:
            return decoded
        decoded += body[line_end + 2 : line_end + 2 + size]
        position = line_end + 2 + size + 2


class RecordingHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server: 'RecordingServer'

    def log_message(self, *args: Any) -> None:
        pass

    def read_body(self) -> bytes:
        if self.headers.get('Transfer-Encoding', '').lower() == 'chunked':
            raw = b''
            while True:
                line = self.rfile.readline()
                raw += line
                size = int(line.split(b';')[0].strip(), 16)
                if size == 0:
                    while self.rfile.readline() not in (b'\r\n', b'\n', b''):
                        pass
                    raw += b'\r\n'
                    break
                raw += self.rfile.read(size + 2)
            body = decode_chunks(raw)
        else:
            body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        if 'aws-chunked' in self.headers.get('Content-Encoding', ''):
            body = decode_chunks(body)
        return body

    def handle_request(self) -> None:
        body = self.read_body()
        url = urllib.parse.urlsplit(self.path)
        headers = dict(self.headers.items())
        scope = CREDENTIAL_SCOPE.search(self.headers.get('Authorization', ''))
        if scope is None:
            call = self.jenkins_call(url, body)
            protocol = 'jenkins'
        else:
            signing_name = scope['signing_name']
            service = SERVICE_BY_SIGNING_NAME.get(signing_name, signing_name)
            protocol = service_model(service).protocol
            if protocol == 'json':
                operation = self.headers.get('X-Amz-Target', '').rpartition('.')[2]
                params = json.loads(body or b'{}')
            else:
                operation, params = rest_operation(service, self.command, url, headers, body)
            call = Call(scope['access_key'], service, operation, params)
        self.server.endpoint.record(call)
        self.send_reply(self.server.endpoint.respond(call), protocol, call)

    def jenkins_call(self, url: urllib.parse.SplitResult, body: bytes) -> Call:
        params: dict[str, Any] = {'query': dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))}
        authorization = self.headers.get('Authorization', '')
        credentials = base64.b64decode(authorization.removeprefix('Basic ')).decode() if authorization.startswith('Basic ') else ''
        params['credentials'] = credentials
        if self.headers.get('Content-Type', '').startswith('application/x-www-form-urlencoded'):
            params['form'] = dict(urllib.parse.parse_qsl(body.decode(), keep_blank_values=True))
        return Call(credentials.partition(':')[0], 'jenkins', f'{self.command} {url.path}', params)

    def send_reply(self, reply: Reply, protocol: str, call: Call) -> None:
        body = reply.body
        headers = dict(reply.headers)
        if isinstance(body, dict | list):
            if protocol in JSON_CONTENT_TYPES:
                version = service_model(call.service).metadata.get('jsonVersion', '1.1')
                headers.setdefault('Content-Type', JSON_CONTENT_TYPES[protocol].format(version=version))
            else:
                headers.setdefault('Content-Type', 'application/json')
            payload = json.dumps(body).encode()
        elif isinstance(body, str):
            payload = body.encode()
        else:
            payload = body or b''
        if protocol == 'rest-xml' and reply.status >= 400 and isinstance(body, dict):
            payload = f'<Error><Code>{body["__type"]}</Code><Message>{body["message"]}</Message></Error>'.encode()
            headers['Content-Type'] = 'application/xml'
        headers.setdefault('Content-Length', str(len(payload)))
        self.send_response(reply.status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = handle_request


class RecordingServer(ThreadingHTTPServer):
    daemon_threads = True
    endpoint: 'RecordingEndpoint'


class RecordingEndpoint:
    """The server, the calls it has recorded in arrival order, and the replies it gives.

    `respond` sees every call and returns its reply. A call it has no reply for gets a 400 naming
    it, so the caller fails loudly rather than reading an empty answer as a real one."""

    def __init__(self, respond: Callable[[Call], Reply | None]) -> None:
        self.calls: list[Call] = []
        self.lock = threading.Lock()
        self.responder = respond
        self.server = RecordingServer(('127.0.0.1', 0), RecordingHandler)
        self.server.endpoint = self
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'

    def record(self, call: Call) -> None:
        with self.lock:
            self.calls.append(call)

    def calls_from(self, caller: str) -> list[Call]:
        with self.lock:
            return [call for call in self.calls if call.caller == caller]

    def respond(self, call: Call) -> Reply:
        reply = self.responder(call)
        return reply if reply is not None else error_reply('UnhandledByStub', f'no reply for {call.service} {call.operation}')

    def __enter__(self) -> 'RecordingEndpoint':
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
