"""Parse git remote URLs into an explicit host/owner/repo target."""

import re
from dataclasses import dataclass

_COMPONENT = re.compile(r"[A-Za-z0-9._-]{1,200}")
_SSH = re.compile(r"^ssh://(?:[^@/]+@)?([^/:]+)(?::\d+)?/([^/]+)/([^/]+?)(?:\.git)?$")
_HTTPS = re.compile(r"^https://([^/:]+)(?::\d+)?/([^/]+)/([^/]+?)(?:\.git)?/?$")
_SCP = re.compile(r"^(?:[^@\s]+@)?([^:\s]+):([^/\s]+)/([^/\s]+?)(?:\.git)?$")
_REMOTE_LINE = re.compile(r"^(\S+)\t(\S+)\s+\((fetch|push)\)$")


def _component(value):
    return bool(_COMPONENT.fullmatch(value)) and not value.startswith("-") and ".." not in value


def parse_git_remote_url(url):
    if not isinstance(url, str):
        return None
    text = url.strip()
    for pattern in (_SSH, _HTTPS, _SCP):
        match = pattern.match(text)
        if not match:
            continue
        host, owner, repo = match.group(1), match.group(2), match.group(3)
        if _component(owner) and _component(repo) and host and "/" not in host and not host.startswith("-"):
            return (host, owner, repo)
    return None


@dataclass
class RemoteChoice:
    slug: str | None
    host: str | None = None
    owner: str | None = None
    repo: str | None = None
    remote_name: str | None = None
    notice: str = ""
    reason: str = ""
    browser_base: str | None = None


def choose_remote(text, preferred, allowed_hosts):
    remotes = {}
    for line in (text or "").splitlines():
        match = _REMOTE_LINE.match(line.strip())
        if not match:
            continue
        name, url, kind = match.group(1), match.group(2), match.group(3)
        if name not in remotes or kind == "fetch":
            remotes[name] = url
    if preferred not in remotes:
        return RemoteChoice(None, reason=f"PRs unavailable: no {preferred} remote")
    parsed = parse_git_remote_url(remotes[preferred])
    if not parsed:
        return RemoteChoice(None, reason=f"PRs unavailable: {preferred} URL is not a repository")
    host, owner, repo = parsed
    allowed = tuple(allowed_hosts or ())
    if host not in allowed:
        return RemoteChoice(
            None,
            reason=f"PRs unavailable: {preferred} host {host} is not an allowed GitHub host",
        )
    notice = ""
    for name, url in remotes.items():
        if name == preferred:
            continue
        other = parse_git_remote_url(url)
        if not other:
            continue
        other_host, other_owner, other_repo = other
        if other_host in allowed and (other_host, other_owner, other_repo) != (host, owner, repo):
            notice = (
                f"querying {preferred} {owner}/{repo}; "
                f"{name} {other_owner}/{other_repo} not queried"
            )
            break
    return RemoteChoice(
        slug=f"{host}/{owner}/{repo}",
        host=host,
        owner=owner,
        repo=repo,
        remote_name=preferred,
        notice=notice,
        browser_base=f"https://{host}/{owner}/{repo}",
    )
