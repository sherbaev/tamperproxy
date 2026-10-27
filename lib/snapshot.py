"""Plain-JSON copies of mitmproxy requests/responses for the panel (inspect + breakpoints).

Taken on the proxy's event loop, so the panel thread never touches a live flow.
"""

from urllib.parse import urlsplit

TEXT_HINTS = ("json", "text", "xml", "html", "javascript", "x-www-form-urlencoded", "graphql")


def _is_text(content_type, data):
    if any(hint in content_type.lower() for hint in TEXT_HINTS):
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def message(msg, max_body):
    """Headers (order and repeats kept) + decoded body; binary or oversized bodies are summarised."""
    if msg is None:
        return None
    data = msg.get_content(strict=False) or b""
    content_type = msg.headers.get("content-type", "")
    is_text = _is_text(content_type, data)
    return {
        "headers": [[name, value] for name, value in msg.headers.items(multi=True)],
        "body": data[:max_body].decode("utf-8", errors="replace") if is_text else None,
        "binary": not is_text,
        "truncated": is_text and len(data) > max_body,
        "size": len(data),
        "content_type": content_type,
    }


def request(flow, max_body):
    req = flow.request
    return {
        **message(req, max_body),
        "method": req.method,
        "url": req.pretty_url,
        "host": req.pretty_host,
        "path": urlsplit(req.pretty_url).path,
    }


def response(flow, max_body):
    if flow.response is None:
        return None
    return {**message(flow.response, max_body), "status": flow.response.status_code, "reason": flow.response.reason}
