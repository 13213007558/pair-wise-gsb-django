"""Human-readable rendering of a Trace decision path."""

from .harness import CSRF_COOKIE_NAME, CSRF_FORM_TOKEN, CSRF_SECRET


def render_trace(trace):
    verdict = "ALLOW (200)" if trace.accepted else f"DENY ({trace.status_code})"
    lines = [
        f"[{trace.entry}] -> {verdict}",
        f"  secure connection : is_secure()={trace.secure}  scheme={trace.scheme!r}",
        f"  request host      : raw={trace.raw_host!r} "
        f"get_host()={trace.host!r} allowed={trace.host_ok}",
        f"  Origin header     : {trace.origin_header or '<absent>'}",
        f"  Referer header    : {trace.referer_header or '<absent>'}",
        f"  trusted origins   : {trace.trusted_origins or '[] (empty)'}",
    ]
    if trace.origin_header:
        lines.append(f"  origin rule hit   : {trace.origin_branch}")
        lines.append(f"  _origin_verified  : {trace.origin_verified}")
    if trace.referer_header or trace.referer_outcome:
        lines.append(
            "  referer check     : "
            f"{trace.referer_outcome or '<not run: Origin present>'}"
        )
    if not trace.accepted:
        lines.append(f"  rejection reason  : {trace.rejection_reason}")
    return "\n".join(lines)


def fixed_credentials_banner():
    return (
        "Fixed legal CSRF credentials reused by every scenario:\n"
        f"  {CSRF_COOKIE_NAME} cookie (secret) : {CSRF_SECRET}\n"
        f"  form csrfmiddlewaretoken (masked)  : {CSRF_FORM_TOKEN}"
    )
