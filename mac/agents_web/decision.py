"""What a guard decides about a request (Allow, Ask, Deny), and the errors the tools raise.

`rule` is a short fixed id for the run log and counters (one of ASK_RULES or DENY_RULES); it never holds request
text. `reason` is one plain sentence for the approval card or the log and never quotes the URL or query. `key` is
exactly what an approval covers: the cleaned URL or the query, never a domain. to_log() is the form to write to a run
log: the same fields with the key (or the refused URL) cut to 200 characters.

A Deny is final. Nothing in these tools turns one into a fetch: approvals match an Ask's key only, a Deny has no key,
and the egress guard refuses the request again on the wire whatever the caller decided (YOLO mode included).
"""
from dataclasses import dataclass

LOG_MAX = 200
ASK_RULES = ("long_query", "encoded_value", "long_params", "notes_copy", "email", "phone")
DENY_RULES = ("scheme", "port", "userinfo", "ip_literal", "name", "address", "bad_url", "too_long", "empty",
              "not_allowed")


def shorten(text, n=LOG_MAX):
    text = str(text or "")
    return text if len(text) <= n else text[:n - 1] + "\u2026"


@dataclass(frozen=True)
class Allow:
    kind = "allow"

    def to_dict(self):
        return {"kind": self.kind}


@dataclass(frozen=True)
class Ask:
    rule: str
    reason: str
    key: str
    kind = "ask"

    def to_dict(self):
        return {"kind": self.kind, "rule": self.rule, "reason": self.reason, "key": self.key}

    def to_log(self):
        return {"kind": self.kind, "rule": self.rule, "reason": self.reason, "key": shorten(self.key)}


@dataclass(frozen=True)
class Deny:
    rule: str
    reason: str
    kind = "deny"

    def to_dict(self):
        return {"kind": self.kind, "rule": self.rule, "reason": self.reason}

    def to_log(self):
        return self.to_dict()


ALLOW = Allow()


class ToolError(Exception):
    pass


class Denied(ToolError):
    """A denied-network attempt: the run log counts it and it raises an alert."""

    def __init__(self, decision, url=""):
        super().__init__(decision.reason)
        self.decision, self.url = decision, url

    def to_log(self):
        return dict(self.decision.to_log(), url=shorten(self.url))


class NeedsApproval(ToolError):
    """the user must approve this exact URL or query (decision.key) first."""

    def __init__(self, decision):
        super().__init__(decision.reason)
        self.decision = decision

    def to_log(self):
        return self.decision.to_log()


class FetchFailed(ToolError):
    def __init__(self, reason, url=""):
        super().__init__(reason)
        self.reason, self.url = reason, url


class ParserUnavailable(ToolError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class SearchUnavailable(ToolError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class FilesDenied(ToolError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason
