"""Local, deterministic filtering before persistence or transmission (ADR-0005)."""

import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from one_memory.input_sanitization import REDACTED_SECRET, sanitize_model_input

POLICY_VERSION = "cmh-credentials-v1"
MAX_CONTENT_BYTES = 1024 * 1024
MAX_DEPTH = 24
MAX_NODES = 20_000
_KEY_NAMES = (
    "password",
    "passwd",
    "pwd",
    "passphrase",
    "secret",
    "clientsecret",
    "token",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "apikey",
    "appkey",
    "accesskey",
    "secretkey",
    "privatekey",
    "authorization",
    "proxyauthorization",
    "cookie",
    "setcookie",
    "sessiontoken",
    "sessioncookie",
    "credential",
    "credentials",
    "auth",
    "bearer",
    "cookies",
    "accesskeyid",
    "secretaccesskey",
    "awsaccesskeyid",
    "awssecretaccesskey",
    "awssessiontoken",
    "密钥",
    "密码",
    "口令",
    "令牌",
    "访问令牌",
    "登录凭证",
)
_SECRET_KEYS = frozenset(_KEY_NAMES)
_MEDIA_KEYS = frozenset(
    {
        "attachment",
        "attachments",
        "image",
        "images",
        "imageurl",
        "audio",
        "video",
        "pdf",
        "filecontent",
    }
)
_TEXT_KEY = (
    r"password|passwd|pwd|passphrase|(?:client[-_ ]?)?secret|"
    r"(?:access[-_ ]?|refresh[-_ ]?|id[-_ ]?|session[-_ ]?)?token|"
    r"(?:x[-_ ]?)?api[-_ ]?key|appkey|access[-_ ]?key|secret[-_ ]?key|"
    r"private[-_ ]?key|credentials?|"
    r"(?:aws[-_ ]?)?(?:secret[-_ ]?)?access[-_ ]?key(?:[-_ ]?id)?|aws[-_ ]?session[-_ ]?token"
)
_ASSIGNMENT = re.compile(
    r"(?i)(?<![\w])(?P<label>[\"']?(?:" + _TEXT_KEY + r")[\"']?\s*[:=]\s*)"
    r"(?P<value>\"(?:\\.|[^\"\\])*(?:\"|\Z)|'(?:\\.|[^'\\])*(?:'|\Z)|[^\s,;&]+)"
)
_CLI = re.compile(
    r"(?i)(--(?:" + _TEXT_KEY + r")(?:=|\s+))(\"[^\"]*(?:\"|\Z)|'[^']*(?:'|\Z)|[^\s]+)"
)
_CHINESE = re.compile(r"((?:密码|口令|密钥|访问令牌|令牌|登录凭证)\s*(?:是|为|[:=：])\s*)[^\r\n]+")
_SENTENCE = re.compile(r"(?i)(\b(?:" + _TEXT_KEY + r")\s+(?:is|was)\s+)[^\r\n]+")
_HEADERS = re.compile(
    r"(?im)(\b(?:authorization|proxy-authorization|set-cookie|cookie)\s*[:=]\s*)"
    r"[^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)*"
)
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
_EXTRA_TOKEN = re.compile(
    r"\b(?:cmhdev_v1_[0-9a-f-]{36}_[A-Za-z0-9_-]{43}|"
    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|glpat-[A-Za-z0-9_-]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|gh[ousr]_[A-Za-z0-9]{20,})\b"
)
_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]{1,20}://[^\s<>\"']+")
_INLINE_MEDIA = re.compile(r"(?i)\bdata:[^\s,]*;base64,")
_JSON_PREFIX = re.compile(r'^\s*(?:\{|\[\s*[\[{"])')
_URL_PLACEHOLDER = re.compile(r"\x00[\U000f0000-\U0010fffd]\x00")


class PrivacyError(Exception):
    """Only fixed reason codes; never retain the rejected input in exceptions."""


def key_name(value: str) -> str:
    name = re.sub(r"[\s_-]", "", unicodedata.normalize("NFKC", value)).lower()
    return name[1:] if name.startswith("xapikey") else name


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PrivacyError("duplicate_json_key")
        result[key] = value
    return result


def parse_json(value: str | bytes):
    try:
        return json.loads(value, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise PrivacyError("invalid_json") from None


@dataclass(frozen=True)
class FilteredContent:
    value: object = field(repr=False)
    redacted: bool
    policy_version: str = POLICY_VERSION


@dataclass(frozen=True)
class CredentialFilter:
    known_credentials: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self):
        if len(self.known_credentials) > 256 or any(
            not isinstance(value, str) or not value or "\0" in value
            for value in self.known_credentials
        ):
            raise PrivacyError("invalid_known_credentials")
        if sum(len(value) for value in self.known_credentials) > 65_536:
            raise PrivacyError("known_credentials_too_large")

    def protect(self, content: object) -> FilteredContent:
        try:
            original = json.dumps(content, ensure_ascii=False, allow_nan=False)
            if len(original.encode()) > MAX_CONTENT_BYTES:
                raise PrivacyError("content_too_large")
            budget = [MAX_NODES]
            result = self._walk(content, depth=0, budget=budget)
            serialized = json.dumps(result, ensure_ascii=False, allow_nan=False)
            if len(serialized.encode()) > MAX_CONTENT_BYTES:
                raise PrivacyError("content_too_large")
            return FilteredContent(result, original != serialized)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise PrivacyError("invalid_content") from None

    def protect_text_fragment(self, text: str) -> str:
        """Filter a bounded page without treating an incomplete JSON prefix as a document.

        The producer must filter the complete original before pagination. Consumers
        reassembling structured data must filter that complete document again.
        """
        try:
            if not isinstance(text, str) or len(text.encode()) > MAX_CONTENT_BYTES:
                raise PrivacyError("content_too_large")
            result = self._text(text, depth=0, budget=[MAX_NODES], structured=False)
            if len(result.encode()) > MAX_CONTENT_BYTES:
                raise PrivacyError("content_too_large")
            return result
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise PrivacyError("invalid_content") from None

    def _walk(self, value, *, depth: int, budget: list[int]):
        budget[0] -= 1
        if depth > MAX_DEPTH or budget[0] < 0:
            raise PrivacyError("content_structure_limit")
        if isinstance(value, str):
            return self._text(value, depth=depth, budget=budget)
        if value is None or type(value) is bool:
            return value
        if type(value) is int or (type(value) is float and math.isfinite(value)):
            return REDACTED_SECRET if str(value) in self.known_credentials else value
        if isinstance(value, list):
            return [self._walk(item, depth=depth + 1, budget=budget) for item in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise PrivacyError("invalid_metadata_key")
                name = key_name(key)
                if name in _MEDIA_KEYS and item not in (None, [], ""):
                    raise PrivacyError("unsupported_media")
                cleaned_key = self._text(key, depth=depth + 1, budget=budget, structured=False)
                if cleaned_key in result:
                    raise PrivacyError("filtered_key_collision")
                result[cleaned_key] = (
                    REDACTED_SECRET
                    if name in _SECRET_KEYS
                    else self._walk(item, depth=depth + 1, budget=budget)
                )
            return result
        raise PrivacyError("unsupported_content_type")

    def _text(self, text: str, *, depth: int, budget: list[int], structured: bool = True) -> str:
        if depth > MAX_DEPTH:
            raise PrivacyError("content_structure_limit")
        if any(
            unicodedata.category(char) in {"Cf", "Cc", "Cs"} and char not in "\n\r\t"
            for char in text
        ):
            raise PrivacyError("hidden_control_character")
        if _INLINE_MEDIA.search(text):
            raise PrivacyError("unsupported_media")
        # Parse nested JSON tool output before text rules; escaped secrets must
        # be decoded, and one sensitive object field must not leak its children.
        if structured and _JSON_PREFIX.match(text):
            nested = parse_json(text)
            return json.dumps(
                self._walk(nested, depth=depth + 1, budget=budget), ensure_ascii=False
            )
        # URL query separators must not be consumed by upstream's generic
        # assignment rule. Protect each parsed URL as one unit while filtering
        # its surrounding text (including e.g. password=<whole URL>).
        urls = {}
        known_characters = set("".join(self.known_credentials))
        marker_index = 0

        def reserve(match):
            nonlocal marker_index
            if len(urls) >= 2048:
                raise PrivacyError("content_structure_limit")
            while chr(0xF0000 + marker_index) in known_characters:
                marker_index += 1
            marker = "\0" + chr(0xF0000 + marker_index) + "\0"
            marker_index += 1
            # Incoming NUL is rejected above. Private-use marker characters
            # absent from known credentials cannot collide or get redacted.
            value = match[0]
            closers = {")": "(", "]": "[", "}": "{"}
            counts = {char: value.count(char) for char in "()[]{}"}
            end = len(value)
            while end and value[end - 1] in closers:
                close = value[end - 1]
                if counts[close] <= counts[closers[close]]:
                    break
                counts[close] -= 1
                end -= 1
            urls[marker] = self._url(value[:end], depth=depth + 1, budget=budget)
            return marker + value[end:]

        text = self._plain_text(_URL.sub(reserve, text))
        return _URL_PLACEHOLDER.sub(lambda match: urls[match[0]], text)

    def _plain_text(self, text: str) -> str:
        text = unicodedata.normalize("NFKC", text)
        for secret in sorted(self.known_credentials, key=len, reverse=True):
            text = REDACTED_SECRET.join(
                part.replace(unicodedata.normalize("NFKC", secret), REDACTED_SECRET)
                for part in text.split(REDACTED_SECRET)
            )
        text = _HEADERS.sub(lambda match: match[1] + REDACTED_SECRET, text)
        text = _BEARER.sub(lambda match: match[1] + " " + REDACTED_SECRET, text)
        text = _CHINESE.sub(lambda match: match[1] + REDACTED_SECRET, text)
        text = _SENTENCE.sub(lambda match: match[1] + REDACTED_SECRET, text)
        text = _CLI.sub(lambda match: match[1] + REDACTED_SECRET, text)
        text = _ASSIGNMENT.sub(lambda match: match["label"] + REDACTED_SECRET, text)
        text = _EXTRA_TOKEN.sub(REDACTED_SECRET, text)
        return sanitize_model_input(text)

    def _url(self, value: str, *, depth: int, budget: list[int]) -> str:
        try:
            parts = urlsplit(value)
            netloc = parts.netloc
            if "@" in netloc:
                netloc = "redacted-secret@" + netloc.rsplit("@", 1)[1]
            netloc = self._walk(unquote(netloc), depth=depth, budget=budget)
            query = [
                (
                    self._walk(key, depth=depth, budget=budget),
                    REDACTED_SECRET
                    if key_name(key) in _SECRET_KEYS
                    else self._walk(value, depth=depth, budget=budget),
                )
                for key, value in parse_qsl(
                    parts.query, keep_blank_values=True, max_num_fields=MAX_NODES
                )
            ]
            path = self._walk(unquote(parts.path), depth=depth, budget=budget)
            fragment = self._walk(unquote(parts.fragment), depth=depth, budget=budget)
            return urlunsplit(
                (
                    parts.scheme,
                    netloc,
                    quote(path, safe="/:@"),
                    urlencode(query),
                    quote(fragment, safe="=&/"),
                )
            )
        except ValueError:
            raise PrivacyError("invalid_url") from None
