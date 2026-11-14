"""Board: Goals and Bugs on GitHub, one API/service layer used by the view and MCP.

Stdlib only. GitHub is the only backend; this app stores just each person's token
(0600, server side, never returned) and one setting (the org). The host gateway owns
authentication; direct ports stay loopback.
"""
import base64
import hashlib
import html as htmllib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import sys
if '__file__' in globals():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import report as reporter  # noqa: E402  0.20: the 'Report or request' form (its own module, a future standalone app)
import feedback_inbox  # noqa: E402  0.26.5: the Feedback app's inbox (Pending approval), through the Feedback relay

ROOT = Path(__file__).resolve().parent.parent if '__file__' in globals() else Path(os.environ.get('APP_ROOT', '.')).resolve()
DATA = Path(os.environ.get('DATA_DIR', ROOT / '.data'))
PRIVATE = DATA / 'private'
SOURCE_REPO = 'humanitylabs-org/wizardos-tracker'  # 0.16.1: moved from oscarhenrycollins/wizardos-tracker to the org
APP_ID = 'wizardos-tracker'  # 0.13.7: id follows the name WizardOS Tracker (was wizard-os-tracker, before that board)
VERSION = '0.33.8'
USER_AGENT = 'wizardos-tracker/' + VERSION + ' (+https://github.com/' + SOURCE_REPO + ')'
GITHUB = os.environ.get('GITHUB_API', 'https://api.github.com').rstrip('/')
DEFAULT_ORG = os.environ.get('BOARD_DEFAULT_ORG', 'humanitylabs-org')
TOKEN_URL = 'https://github.com/settings/personal-access-tokens/new'
# 0.13: Sign in with GitHub (OAuth device flow). No client secret: only a public client id, set by a team
# member (POST /api/settings/github-client-id), in DATA_DIR/policy.json `github_client_id`, or env GITHUB_CLIENT_ID.
GITHUB_WEB = os.environ.get('GITHUB_WEB', 'https://github.com').rstrip('/')
DEVICE_SCOPE = 'repo read:org'  # classic OAuth App only; a GitHub App's permissions come from the app, not a scope
CLIENT_ID = re.compile(r'^[A-Za-z0-9._-]{8,80}$')
# Only these people may open Goals in the app (GitHub logins, comma separated).
GOAL_AUTHORS = {x.strip().lower() for x in os.environ.get('GOAL_AUTHORS', 'jdefleur').split(',') if x.strip()}
# The builder agent's GitHub account: it labels finished work ready-to-test and says what to check.
BUILDER = os.environ.get('BOARD_BUILDER', 'wizardos-builder[bot]').strip().lower()
# 0.32 (12 Nov): the builder is the GitHub App WizardOS Builder. REST names it wizardos-builder[bot], GraphQL
# wizardos-builder, search app/wizardos-builder; the suspended wizardosbuilder user still counts for old items.
BUILDERS = {BUILDER, 'wizardos-builder[bot]', 'wizardos-builder', 'app/wizardos-builder', 'wizardosbuilder'}
DEFAULT_REPO = os.environ.get('BOARD_DEFAULT_REPO', 'wizard-ux')
MEDIA_REPO = os.environ.get('BOARD_MEDIA_REPO', 'humanitylabs-org/wizard-issue-media')
# 0.12: the human UI is for the team only. Signing in needs an active member of this org, and a person's
# own GitHub account (agent and builder accounts are refused; agents use the MCP tools instead).
TEAM_ORG = os.environ.get('BOARD_TEAM_ORG', 'humanitylabs-org').strip()
AGENT_LOGINS = {'oscarhenrycollins', 'defleur-agents'} | BUILDERS
MEMBER_SECONDS = 600
# 0.24.0: plain one-liners a new team member understands (no org jargon up front, no token talk for GitHub sign-in)
NOT_MEMBER = 'This is for the WizardOS team, and this GitHub account isn\'t on it yet. Ask Miguel or James to add you, then sign in again.'
INVITE_PENDING = (f'You have a team invite waiting on GitHub. Accept it at https://github.com/orgs/{TEAM_ORG}/invitation, '
                  'then sign in again.')
# 0.24.1: what a stock agent hears when nobody has signed in yet, and how the Tracker stays current (never wam)
NOBODY_AGENT = 'Nobody has signed in to WizardOS Tracker on this Wizard yet. Ask the owner to open WizardOS Tracker and tap Sign in with GitHub.'
AGENT_UPDATE = ('Signing in and updating are the owner\'s taps, never yours. Not signed in: ask the owner to open WizardOS Tracker and tap Sign in with GitHub. '
                'A newer Tracker: its main screen shows an Update button; send the owner there (other apps and WizardOS: Tracker Settings, Update). '
                'Never run wam, docker or install commands, never ask for a token and never point anyone at config files. ')
NOT_HUMAN = "Sign in with your own GitHub account, not your agent's. Agents use the Tracker's tools instead."
CACHE_SECONDS = 60
STATS_SECONDS = 60  # 0.13.9: was 300; the arrows must follow the builder in near real time
STATIC_TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml'}
NAME = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})$')
TASK = re.compile(r'^(\s*[-*+]\s+\[)([ xX])(\]\s?)(.*)$')
LOCK = threading.Lock()


TOKEN_DEAD = 'GitHub says your saved sign-in no longer works (it was revoked, regenerated or has expired). Sign in again.'


class Problem(Exception):
    def __init__(self, code, message, reason=None):
        super().__init__(message)
        self.code, self.message, self.reason = code, message, reason


# ---------- private storage (0700 dir, 0600 files) ----------

def _private():
    PRIVATE.mkdir(parents=True, exist_ok=True)
    os.chmod(PRIVATE, 0o700)
    return PRIVATE


def _read(name, default):
    try:
        return json.loads((_private() / name).read_text())
    except (FileNotFoundError, ValueError, OSError):
        return default


def _write(name, value):
    d = _private()
    tmp = d / (name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, indent=1)
    os.chmod(tmp, 0o600)
    tmp.replace(d / name)


def secure_start():
    for p in _private().iterdir():
        if p.is_file():
            os.chmod(p, 0o600)


def accounts():
    a = _read('accounts.json', {})
    if not isinstance(a, dict):
        a = {}
    a.setdefault('people', {})
    return a


def settings():
    s = _read('settings.json', {})
    org = s.get('org') if isinstance(s, dict) and isinstance(s.get('org'), str) and NAME.match(s['org']) else DEFAULT_ORG
    return {'org': org}


def person_key(person):
    p = (person or '').strip().lower()
    return p if p and len(p) <= 200 else 'owner'


def account_for(person=None, agent=False):
    """The token used for one request. People use their own; agents (MCP) use the
    box's default account: the first person who connected."""
    a = accounts()
    people = a['people']
    if agent:
        key = a.get('default') if a.get('default') in people else next(iter(people), None)
    else:
        key = person_key(person)
    acct = people.get(key) if key else None
    if not acct or not acct.get('token'):
        raise Problem(401, NOBODY_AGENT if agent else 'Sign in with GitHub first.')
    if acct.get('refresh_token') and _due(acct):
        acct = refresh_account(key, acct) or acct
    if not agent:
        try:
            _check_person(key, acct)
        except Problem as e:
            fresh = e.reason == 'token_dead' and acct.get('refresh_token') and refresh_account(key, acct, force=True)
            if not fresh:
                raise
            acct = fresh
            _check_person(key, acct)
    return key, acct


# ---------- 0.15: GitHub App user tokens expire (8 h); the refresh token (6 months) renews them silently ----------
# Device-flow tokens of a GitHub App refresh with the client id alone (no client secret). A refresh token works once:
# the new pair replaces the old one at once, under REFRESH_LOCK, so two requests never spend the same refresh token.

REFRESH_EARLY = 15 * 60   # renew when less than this is left
REFRESH_EVERY = 5 * 60    # background check, so idle boxes (and the agent's tools) stay signed in
REFRESH_LOCK = threading.Lock()
_renamed = {}  # token id -> the token that replaced it


def _due(acct, now=None):
    exp = acct.get('expires_at')
    return bool(exp) and exp - (now or time.time()) < REFRESH_EARLY


def _token_fields(d, now=None):
    """The parts of a GitHub token answer the Tracker keeps (refresh fields only when GitHub sent them)."""
    now = now or time.time()
    out = {}
    if isinstance(d.get('refresh_token'), str) and d['refresh_token']:
        out['refresh_token'] = d['refresh_token']
    for k, to in (('expires_in', 'expires_at'), ('refresh_token_expires_in', 'refresh_expires_at')):
        try:
            if d.get(k) is not None:
                out[to] = int(now + int(d[k]))
        except (TypeError, ValueError):
            pass
    return out


def refresh_account(key, acct=None, force=False):
    """Swap a person's refresh token for a new token pair. -> the updated account, or None (no refresh token,
    GitHub refused it, or GitHub could not be reached). `acct`: the copy the caller holds; if the saved token has
    moved on already, the saved account is returned without spending anything."""
    with REFRESH_LOCK:
        cur = accounts()['people'].get(key)
        if not cur or not cur.get('refresh_token'):
            return None
        if acct is not None and cur.get('token') != acct.get('token'):
            return cur
        if not force and not _due(cur):
            return cur
        if cur.get('refresh_expires_at') and cur['refresh_expires_at'] <= time.time():
            return None
        code, d = _gh_form(GITHUB_WEB + '/login/oauth/access_token', {'client_id': cur.get('client_id') or github_client_id() or '',
                                                                      'grant_type': 'refresh_token', 'refresh_token': cur['refresh_token']})
        d = d if isinstance(d, dict) else {}
        if not d.get('access_token'):
            if d.get('error') in ('bad_refresh_token', 'unauthorized_client', 'incorrect_client_credentials'):
                with LOCK:
                    a = accounts()
                    if (a['people'].get(key) or {}).get('refresh_token') == cur['refresh_token']:
                        a['people'][key].pop('refresh_token', None)  # spent or revoked: the next sign-in replaces it
                        a['people'][key]['refresh_failed'] = d.get('error')
                        _write('accounts.json', a)
            return None
        new = {**cur, 'token': d['access_token'], 'refreshed_at': int(time.time())}
        new.pop('refresh_failed', None)
        new.update(_token_fields(d))
        with LOCK:
            a = accounts()
            a['people'][key] = new
            _write('accounts.json', a)
            for k, v in list(_renamed.items()):
                if v == cur['token']:
                    _renamed[k] = new['token']
            _renamed[_tid(cur['token'])] = new['token']
        _forget(cur['token'])
        return new


def refresh_token_of(token):
    """gh() got a 401 for `token`: renew the account that holds it. -> the new token, or None."""
    if _tid(token) in _renamed:
        return _renamed[_tid(token)]
    for key, acct in accounts()['people'].items():
        if acct.get('token') == token and acct.get('refresh_token'):
            new = refresh_account(key, acct, force=True)
            return new['token'] if new and new.get('token') != token else None
    return None


def refresh_loop(sleep=time.sleep, rounds=None):
    """Renew every saved GitHub App token shortly before it expires (also refreshes the 6-month refresh token)."""
    n = 0
    while rounds is None or n < rounds:
        n += 1
        for key, acct in list(accounts()['people'].items()):
            if acct.get('refresh_token') and _due(acct):
                try:
                    refresh_account(key, acct)
                except Exception:  # noqa: BLE001 - one bad account never stops the loop
                    pass
        try:
            _retry_pending()   # 0.24.2: screenshots that could not upload yet
        except Exception:  # noqa: BLE001 - never stops the loop
            pass
        if rounds is None or n < rounds:
            sleep(REFRESH_EVERY)


# ---------- 0.12: team-only, human-only (the human UI; MCP tools are unchanged) ----------

_members = {}  # (token id, org, login) -> (checked at, member?)
_limited = set()  # keys whose token could not read the org membership API (403), for a clearer refusal


_pending = set()  # 0.24.0: keys whose org membership is an invite not yet accepted


def not_member_problem(token, login):
    key = (_tid(token), TEAM_ORG.lower(), (login or '').lower())
    if key in _pending:
        return Problem(403, INVITE_PENDING, 'not_member')
    # token advice only for a pasted token: GitHub sign-in (ghu_/gho_) never made one, so never talk about tokens
    if key in _limited and not str(token).startswith(('ghu_', 'gho_')):
        return Problem(403, NOT_MEMBER + f' If you are on the team already, make the token with Resource owner {TEAM_ORG} '
                       '(or make your membership public on GitHub), then sign in again.', 'not_member')
    return Problem(403, NOT_MEMBER, 'not_member')


def agent_logins(base=None):
    """Agent and builder accounts: the fixed set, the roster's agents and builder, and policy.json `agents`."""
    out = set(AGENT_LOGINS) | BUILDERS
    try:
        base = base or policy_base()
        out |= {a['login'].lower() for t in base['team'] for a in t['agents']}
        out |= {x.lower() for x in base['builder']['logins']}
    except Exception:  # noqa: BLE001 -- a broken policy file never opens the gate wider
        pass
    extra = _policy_file().get('agents')
    if isinstance(extra, list):
        out |= {(x.get('login') if isinstance(x, dict) else x).lower() for x in extra
                if isinstance(x.get('login') if isinstance(x, dict) else x, str)}
    return out


def is_agent_login(login):
    return (login or '').strip().lower() in agent_logins()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_no_redirect = urllib.request.build_opener(_NoRedirect)


def _gh_code(token, path):
    """(status, json or None) for one GET, no redirects followed, nothing cached. Network trouble -> (0, None)."""
    req = urllib.request.Request(GITHUB + path, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                                                         'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': USER_AGENT})
    try:
        with _no_redirect.open(req, timeout=15) as r:
            raw = r.read(1 << 20)
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return 0, None


def _gql_member(token):
    """GraphQL organization.viewerIsAMember: True/False, None when GitHub gave no answer.
    Works for fine-grained tokens, which REST treats as outsiders without the org Members permission."""
    req = urllib.request.Request(GITHUB + '/graphql', data=json.dumps({'query': 'query($o: String!) { organization(login: $o) { viewerIsAMember } }',
                                                                       'variables': {'o': TEAM_ORG}}).encode(), method='POST',
                                 headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', 'User-Agent': USER_AGENT})
    try:
        with _no_redirect.open(req, timeout=15) as r:
            out = json.loads(r.read(1 << 20) or b'{}')
    except urllib.error.HTTPError as e:
        return 'bad' if e.code == 401 else None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    v = ((out.get('data') or {}).get('organization') or {}).get('viewerIsAMember') if isinstance(out, dict) else None
    return v if isinstance(v, bool) else None


def team_member(token, login, fresh=False):
    """Is `login` (the token's own user) an active member of TEAM_ORG? Cached 10 minutes.
    Checks in order, the first yes wins: GET /user/memberships/orgs/{org} state active; GraphQL
    viewerIsAMember; GET /orgs/{org}/members/{login} 204 (a 302 means GitHub sees the token as an outsider,
    then /public_members/{login} 204). No only on a definite answer; GitHub down or rate-limited raises 502
    (or keeps the last answer) and never signs anyone out."""
    key = (_tid(token), TEAM_ORG.lower(), (login or '').lower())
    with LOCK:
        hit = _members.get(key)
    if hit and not fresh and time.time() - hit[0] < MEMBER_SECONDS:
        return hit[1]
    code, body = _gh_code(token, f'/user/memberships/orgs/{TEAM_ORG}')
    answered = False
    ok = False
    if code == 200 and isinstance(body, dict):
        answered = True
        ok = body.get('state') == 'active' and ((body.get('user') or {}).get('login') or login).lower() == (login or '').lower()
        if body.get('state') == 'pending':
            _pending.add(key)
        else:
            _pending.discard(key)
    if not ok:
        g = _gql_member(token)
        if g == 'bad' or code == 401:
            raise Problem(401, TOKEN_DEAD, 'token_dead')
        if g is True:
            ok = True
        elif g is False:
            answered = True
    if not ok:
        code2, _ = _gh_code(token, f'/orgs/{TEAM_ORG}/members/{login}')
        if code2 == 204:
            ok = True
        elif code2 == 302:
            code3, _ = _gh_code(token, f'/orgs/{TEAM_ORG}/public_members/{login}')
            ok = code3 == 204
            answered = answered or code3 in (204, 404)
        elif code2 == 404:
            answered = True
    if not ok and code == 403:
        _limited.add(key)  # the token can't read org membership (scope/resource owner): say so, don't blame the person
    if not ok and not answered:
        if hit:
            return hit[1]
        raise Problem(502, 'Could not check your team membership with GitHub. Try again in a minute.')
    with LOCK:
        _members[key] = (time.time(), ok)
    return ok


def _sign_out(key, why):
    with LOCK:
        a = accounts()
        old = a['people'].pop(key, None)
        if old is None:
            return
        if a.get('default') == key:
            a['default'] = next(iter(a['people']), None)
        _write('accounts.json', a)
    if old.get('token'):
        _forget(old['token'])


def _check_person(key, acct):
    """A saved account that is an agent's, or whose user left the team, is signed out on its next use."""
    login = acct.get('login') or ''
    if is_agent_login(login):
        _sign_out(key, 'agent')
        raise Problem(403, NOT_HUMAN, 'agent')
    if not team_member(acct['token'], login):
        _sign_out(key, 'not_member')
        raise not_member_problem(acct['token'], login)


def gate(person):
    """Every human write (and every human read through account_for): a signed-in, human team member."""
    return account_for(person)


def public_account(acct):
    out = {k: acct.get(k) for k in ('login', 'name', 'html_url', 'kind', 'scopes', 'saved_at')}
    out['renews'] = bool(acct.get('refresh_token'))
    return out


# ---------- GitHub client (60 s read cache per token) ----------

_cache = {}


def _tid(token):
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _forget(token):
    t = _tid(token)
    with LOCK:
        for k in [k for k in _cache if k[0] == t]:
            _cache.pop(k, None)


GH_FLAKY = (500, 502, 503, 504)
GH_RETRY_PAUSE = 0.8   # seconds before the one retry of a read GitHub answered with a 5xx


def gh(token, method, path, body=None, cache=True, accept='application/vnd.github+json', _retry=True):
    token = _renamed.get(_tid(token), token)  # a request that still holds a token refreshed meanwhile
    url = GITHUB + path
    key = (_tid(token), method, url, json.dumps(body, sort_keys=True) if body is not None else '', accept)
    if cache and method in ('GET', 'GRAPHQL'):
        with LOCK:
            hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
    data = None if body is None else json.dumps(body).encode()
    headers = {'Authorization': 'Bearer ' + token, 'Accept': accept,
               'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': USER_AGENT}
    if data is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, method='POST' if method == 'GRAPHQL' else method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = r.read(8 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        try:
            msg = str(json.loads(e.read(65536) or b'{}').get('message') or '')
        except ValueError:
            msg = ''
        if e.code == 401:
            new = _retry and refresh_token_of(token)  # 0.15: an expired GitHub App token renews itself once
            if new:
                return gh(new, method, path, body, cache, accept, _retry=False)
            raise Problem(401, TOKEN_DEAD, 'token_dead') from None
        if e.code in (403, 429) and 'rate limit' in msg.lower():
            raise Problem(429, 'GitHub rate limit reached. Try again in a minute.') from None
        if e.code in (403, 404):
            raise Problem(e.code, 'Your GitHub token cannot do that here' + (f' ({msg}).' if msg else '.')) from None
        if e.code == 422:
            raise Problem(400, 'GitHub refused the change' + (f': {msg}' if msg else '.')) from None
        if e.code in GH_FLAKY and method in ('GET', 'GRAPHQL') and _retry is not None:
            # 0.30.1: GitHub answers a brief 5xx now and then (often on GraphQL); a read is safe to ask again once.
            time.sleep(GH_RETRY_PAUSE)
            return gh(token, method, path, body, cache, accept, _retry=None)
        raise Problem(502, f'GitHub answered {e.code}' + (f': {msg}' if msg else '.')) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Problem(502, 'Could not reach GitHub. Check the internet connection and try again.') from None
    out = json.loads(payload or b'null')
    if method == 'GRAPHQL' and isinstance(out, dict) and out.get('errors') and not out.get('data'):
        raise Problem(502, ('GitHub: ' + '; '.join(str(e.get('message', '')) for e in out['errors']))[:300])
    if method in ('GET', 'GRAPHQL'):
        if cache:
            with LOCK:
                _cache[key] = (time.time(), out)
                if len(_cache) > 500:
                    for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
                        _cache.pop(k, None)
    else:
        _forget(token)
    return out


def graphql(token, query, variables, cache=True):
    return refiled(gh(token, 'GRAPHQL', '/graphql', {'query': query, 'variables': variables}, cache=cache))


# 0.21: a report the builder's triage step had to copy (GitHub can't transfer an issue from the private wizard-os to a
# public repo) is opened by the builder with one line `Filed by @<login> with Report or request`. The Tracker treats
# that person as its author everywhere (who checks it, Your turn, filters), as if the issue had been transferred.
REFILED = re.compile(r'^Filed by @([A-Za-z0-9](?:[A-Za-z0-9-]{0,38})) with Report or request\b', re.M)


def refiled(data):
    stack = [data]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            a = x.get('author')
            if isinstance(a, dict) and (a.get('login') or '').lower() in BUILDERS and isinstance(x.get('body'), str):
                m = REFILED.search(x['body'])
                if m:
                    x['author'] = {**a, 'login': m.group(1)}
            stack.extend(v for v in x.values() if isinstance(v, (dict, list)))
        elif isinstance(x, list):
            stack.extend(v for v in x if isinstance(v, (dict, list)))
    return data


# ---------- account ----------

def connect(person, args, via='token', client_id=None):
    token = args.get('token')
    if not isinstance(token, str) or not 20 <= len(token.strip()) <= 400 or any(c.isspace() for c in token.strip()):
        raise Problem(400, 'Paste a GitHub personal access token (it starts with github_pat_ or ghp_).')
    token = token.strip()
    req = urllib.request.Request(GITHUB + '/user', headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                                                            'User-Agent': USER_AGENT, 'X-GitHub-Api-Version': '2022-11-28'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            user = json.loads(r.read(1 << 20))
            scopes = r.headers.get('X-OAuth-Scopes')
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise Problem(400, 'GitHub did not accept this token. Check it was copied whole and has not expired.') from None
        raise Problem(502, f'GitHub answered {e.code}. Try again.') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Problem(502, 'Could not reach GitHub to check the token. Try again.') from None
    login = user.get('login') or ''
    # 0.12: team-only and human-only. Nothing is stored for a refused sign-in.
    if is_agent_login(login):
        raise Problem(403, NOT_HUMAN, 'agent')
    if not team_member(token, login):
        raise not_member_problem(token, login)
    kind = ('fine-grained' if token.startswith('github_pat_') else 'classic' if token.startswith('ghp_')
            else 'github-app' if token.startswith('ghu_') else 'oauth' if token.startswith('gho_') or via == 'github' else 'token')
    acct = {'token': token, 'login': user.get('login'), 'name': user.get('name'), 'avatar_url': user.get('avatar_url'),
            'html_url': user.get('html_url'), 'kind': kind, 'scopes': scopes, 'saved_at': int(time.time()), 'via': via}
    if via == 'github':  # 0.15: a GitHub App's expiring token comes with a refresh token
        acct.update(_token_fields(args))
        if client_id:
            acct['client_id'] = client_id
    with LOCK:
        a = accounts()
        key = person_key(person)
        a['people'][key] = acct
        if a.get('default') not in a['people']:
            a['default'] = key
        _write('accounts.json', a)
    return status(person)


# ---------- 0.13: Sign in with GitHub (OAuth device flow, no client secret) ----------

_device = {}  # person key -> flow (device_code stays on the box, never returned)


# A client id is public, not a secret. 0.15: the humanitylabs-org 'WizardOS Tracker' GitHub App (Device Flow, expiring
# user tokens, installed on the org only) becomes the default once it exists; the classic OAuth App stays as the fallback
# (its 'repo' scope reaches every repo a person has, and GitHub keeps only 10 of its tokens per person, so new sign-ins
# on other boxes silently revoke old ones). 0.16.1: the GitHub App exists (app id 5676161, installed on humanitylabs-org)
# and is the built-in default; env GITHUB_APP_CLIENT_ID still overrides. People already signed in through the OAuth App
# keep their saved token (it never refreshes and needs no client id) until they sign in again.
GITHUB_APP_CLIENT_ID = os.environ.get('GITHUB_APP_CLIENT_ID') or 'Iv23li1x6wuDUDj6Herx'
LEGACY_OAUTH_CLIENT_ID = 'Ov23liRmzoKiwejdVDOo'
DEFAULT_GITHUB_CLIENT_ID = GITHUB_APP_CLIENT_ID or LEGACY_OAUTH_CLIENT_ID


def is_github_app_client(cid):
    """GitHub App client ids start Iv1./Iv23; OAuth Apps Ov23 (or 20 hex characters, older ones)."""
    return isinstance(cid, str) and cid.startswith('Iv')


def github_client_id():
    """policy.json `github_client_id`, else env GITHUB_CLIENT_ID, else the team's built-in app."""
    for v in (_policy_file().get('github_client_id'), os.environ.get('GITHUB_CLIENT_ID'), DEFAULT_GITHUB_CLIENT_ID):
        if isinstance(v, str) and CLIENT_ID.match(v.strip()):
            return v.strip()
    return None


def client_id_info():
    pv = _policy_file().get('github_client_id')
    src = 'policy' if isinstance(pv, str) and CLIENT_ID.match(pv.strip()) else 'env' if os.environ.get('GITHUB_CLIENT_ID') else 'built-in' if github_client_id() else None
    return {'set': bool(src), 'client_id': github_client_id(), 'source': src, 'scope': '' if is_github_app_client(github_client_id()) else DEVICE_SCOPE,
            'kind': 'github-app' if is_github_app_client(github_client_id()) else 'oauth-app', 'fallback': LEGACY_OAUTH_CLIENT_ID,
            'note': 'A GitHub App (or OAuth App) client id with Device Flow enabled. No client secret is used or stored.'}


def set_client_id(person, args):
    """POST /api/settings/github-client-id {client_id}: team members only (gated); '' clears it."""
    v = args.get('client_id')
    if not isinstance(v, str) or (v.strip() and not CLIENT_ID.match(v.strip())):
        raise Problem(400, 'Paste the OAuth App client id (letters, digits, dots; for example Ov23li…). Never the client secret.')
    if v.strip().lower().startswith(('ghp_', 'gho_', 'github_pat_')) or len(v.strip()) == 40 and re.fullmatch(r'[0-9a-f]{40}', v.strip()):
        raise Problem(400, 'That looks like a token or a client secret. Paste only the client id.')
    with LOCK:
        over = _policy_file()
        if v.strip():
            over['github_client_id'] = v.strip()
        else:
            over.pop('github_client_id', None)
        over['github_client_id_changed'] = {'by': (accounts()['people'].get(person_key(person)) or {}).get('login') or person_key(person), 'at': int(time.time())}
        _write_policy(over)
    return client_id_info()


def _gh_form(url, fields):
    """POST a form to github.com (device flow endpoints), JSON back. -> (status, dict or None)."""
    req = urllib.request.Request(url, data=urlencode(fields).encode(), method='POST',
                                 headers={'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded',
                                          'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read(1 << 16) or b'{}')
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read(1 << 16) or b'{}')
        except ValueError:
            return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return 0, None


DEVICE_ERRORS = {
    'device_flow_disabled': 'Device flow is turned off for this GitHub app. An owner turns on "Enable Device Flow" in its settings.',
    'incorrect_client_credentials': 'GitHub does not know this client id. Check the id in the Tracker settings.',
    'unsupported_grant_type': 'GitHub refused the sign-in request.', 'incorrect_device_code': 'GitHub lost this sign-in. Start again.',
    'access_denied': 'You cancelled on GitHub. Start again to sign in.', 'expired_token': 'The code expired. Start again.',
}


def device_public(flow, person=None):
    if not flow:
        return {'state': 'none', 'oauth': bool(github_client_id())}
    out = {k: flow.get(k) for k in ('state', 'user_code', 'verification_uri', 'interval', 'message', 'reason')}
    out['expires_in'] = max(0, int(flow['expires_at'] - time.time()))
    if flow['state'] == 'done':
        out['status'] = status(person)
    return out


def _device_code(cid):
    fields = {'client_id': cid} if is_github_app_client(cid) else {'client_id': cid, 'scope': DEVICE_SCOPE}
    code, d = _gh_form(GITHUB_WEB + '/login/device/code', fields)
    return code, d if isinstance(d, dict) else {}


def device_start(person):
    """POST /api/device/start: ask GitHub for a device code; the box polls for the token in the background."""
    cid = github_client_id()
    if not cid:
        raise Problem(404, 'Sign in with GitHub is not set up on this box yet. Use a token instead.', 'no_client_id')
    code, d = _device_code(cid)
    if (code != 200 or not d.get('device_code')) and cid != LEGACY_OAUTH_CLIENT_ID and d.get('error') in ('device_flow_disabled', 'incorrect_client_credentials', 'unauthorized_client'):
        cid = LEGACY_OAUTH_CLIENT_ID  # 0.15: the GitHub App is not set up right: the classic OAuth App still signs people in
        code, d = _device_code(cid)
    if code != 200 or not d.get('device_code') or not d.get('user_code'):
        err = d.get('error')
        raise Problem(502, DEVICE_ERRORS.get(err) or ('Could not reach GitHub. Try again.' if not code else f'GitHub answered {code}' + (f' ({err}).' if err else '.')), err)
    uri = d.get('verification_uri') if str(d.get('verification_uri') or '').startswith('https://') else 'https://github.com/login/device'
    flow = {'id': secrets.token_hex(8), 'device_code': d['device_code'], 'user_code': str(d['user_code'])[:20], 'verification_uri': uri,
            'interval': max(5, int(d.get('interval') or 5)), 'expires_at': time.time() + max(60, min(int(d.get('expires_in') or 900), 1800)),
            'state': 'pending', 'message': '', 'reason': None, 'client_id': cid, 'person': person}
    key = person_key(person)
    with LOCK:
        old = _device.get(key)
        if old and old['state'] == 'pending':
            old['state'] = 'cancelled'
        _device[key] = flow
    threading.Thread(target=device_run, args=(flow,), daemon=True).start()
    return device_public(flow, person)


def device_poll_once(flow, now=None, wait=None):
    """One poll of POST /login/oauth/access_token. Returns seconds to wait before the next poll, or None when the
    flow finished (flow['state'] = done | refused | expired | denied | error | cancelled). Respects interval and slow_down."""
    if flow['state'] != 'pending':
        return None
    now = now if now is not None else time.time()
    if now >= flow['expires_at']:
        flow.update(state='expired', message=DEVICE_ERRORS['expired_token'], device_code=None)
        return None
    code, d = _gh_form(GITHUB_WEB + '/login/oauth/access_token', {'client_id': flow['client_id'], 'device_code': flow['device_code'],
                                                                  'grant_type': 'urn:ietf:params:oauth:grant-type:device_code'})
    d = d if isinstance(d, dict) else {}
    if flow['state'] != 'pending':  # cancelled while we waited
        return None
    if d.get('access_token'):
        flow['device_code'] = None
        try:
            connect(flow['person'], {**d, 'token': d['access_token']}, via='github', client_id=flow['client_id'])  # the same checks as a pasted token
            flow.update(state='done', message='Signed in.')
        except Problem as e:
            flow.update(state='refused' if e.reason in ('agent', 'not_member') else 'error', message=e.message, reason=e.reason)
        return None
    err = d.get('error')
    if err == 'authorization_pending':
        return flow['interval']
    if err == 'slow_down':
        flow['interval'] = max(flow['interval'] + 5, int(d.get('interval') or 0))
        return flow['interval']
    if err in ('expired_token', 'access_denied', 'incorrect_device_code', 'device_flow_disabled', 'incorrect_client_credentials', 'unsupported_grant_type'):
        flow.update(state='expired' if err == 'expired_token' else 'denied' if err == 'access_denied' else 'error',
                    message=DEVICE_ERRORS[err], reason=err, device_code=None)
        return None
    # GitHub unreachable or an unknown answer: keep trying (gently) until the code expires
    return flow['interval'] + (5 if not code or code >= 500 else 0)


def device_run(flow, sleep=time.sleep):
    wait = flow['interval']
    while wait is not None:
        sleep(wait)
        wait = device_poll_once(flow)


def device_status(person):
    with LOCK:
        flow = _device.get(person_key(person))
    return device_public(flow, person)


def device_cancel(person):
    with LOCK:
        flow = _device.pop(person_key(person), None)
    if flow and flow['state'] == 'pending':
        flow.update(state='cancelled', device_code=None)
    return {'state': 'none', 'oauth': bool(github_client_id())}


def disconnect(person):
    with LOCK:
        a = accounts()
        key = person_key(person)
        old = a['people'].pop(key, None)
        if a.get('default') == key:
            a['default'] = next(iter(a['people']), None)
        _write('accounts.json', a)
    if old and old.get('token'):
        _forget(old['token'])
    return status(person)


def status(person=None):
    refused = notice = None
    dead = False
    try:
        gate(person)
    except Problem as e:
        if e.reason in ('agent', 'not_member'):
            refused = {'reason': e.reason, 'message': e.message}
        elif e.reason == 'token_dead':
            dead, notice = True, e.message  # kept on file; a new sign-in replaces it
        # anything else (GitHub down, 502): the session stays as it is
    a = accounts()
    acct = None if dead else a['people'].get(person_key(person))
    return {'refused': refused, 'notice': notice, 'team_org': TEAM_ORG, 'connected': bool(acct and acct.get('token')), 'account': public_account(acct) if acct else None,
            'org': settings()['org'], 'token_url': TOKEN_URL, 'oauth': bool(github_client_id()),
            'agent_account': (a['people'].get(a.get('default')) or {}).get('login'), 'version': VERSION,
            'can_goal': bool(acct and (acct.get('login') or '').lower() in GOAL_AUTHORS),
            'default_repo': DEFAULT_REPO, 'screenshots': bool(acct and acct.get('token') and _media_ready(acct['token']))}


def _media_ready(token):
    """Screenshots need the shared media repo. Board never creates it; it only checks it exists."""
    try:
        gh(token, 'GET', '/repos/' + MEDIA_REPO)
        return True
    except Problem:
        return False


def set_org(args):
    org = args.get('org')
    if not isinstance(org, str) or not NAME.match(org.strip()):
        raise Problem(400, 'Use a GitHub organisation or user name.')
    with LOCK:
        cur = _read('settings.json', {})
        _write('settings.json', {**(cur if isinstance(cur, dict) else {}), 'org': org.strip()})
    return {'org': settings()['org']}


# ---------- board ----------

def tasks_of(body):
    out = []
    for line in (body or '').splitlines():
        m = TASK.match(line)
        if m:
            out.append({'index': len(out), 'done': m.group(2) in 'xX', 'text': m.group(4).strip()})
    return out


ISSUE_FIELDS = '''
  number title url state stateReason body createdAt updatedAt closedAt
  author { login }
  repository { nameWithOwner name }
  labels(first: 20) { nodes { name } }
  closedByPullRequestsReferences(first: 20, includeClosedPrs: true) { nodes { number title url state repository { nameWithOwner } } }
  timelineItems(itemTypes: [CROSS_REFERENCED_EVENT, CONNECTED_EVENT], first: 50) { nodes {
    ... on CrossReferencedEvent { source { ... on PullRequest { number title url state repository { nameWithOwner } } } }
    ... on ConnectedEvent { subject { ... on PullRequest { number title url state repository { nameWithOwner } } } }
  } }
'''
SEARCH = ('query($q: String!) { search(query: $q, type: ISSUE, first: 100) { issueCount nodes { ... on Issue {'
          + ISSUE_FIELDS + ' comments { totalCount } } } } }')
ONE = ('query($o: String!, $r: String!, $n: Int!) { repository(owner: $o, name: $r) { issue(number: $n) {'
       + ISSUE_FIELDS + ' comments(last: 30) { totalCount nodes { author { login } body createdAt url } } } } }')


def _prs(node):
    seen = {}
    for p in (node.get('closedByPullRequestsReferences') or {}).get('nodes') or []:
        if p and p.get('url'):
            seen[p['url']] = p
    for t in (node.get('timelineItems') or {}).get('nodes') or []:
        p = (t or {}).get('source') or (t or {}).get('subject')
        if p and p.get('url'):
            seen[p['url']] = p
    return [{'number': p['number'], 'title': p.get('title'), 'url': p['url'], 'state': (p.get('state') or '').lower(),
             'repo': (p.get('repository') or {}).get('nameWithOwner')} for p in seen.values()]


def shape(node):
    labels = [l['name'] for l in (node.get('labels') or {}).get('nodes') or []]
    low = [l.lower() for l in labels]
    tasks = tasks_of(node.get('body'))
    prs = _prs(node)
    return {'id': f"{node['repository']['nameWithOwner']}#{node['number']}", 'repo': node['repository']['nameWithOwner'],
            'number': node['number'], 'title': node.get('title'), 'url': node.get('url'),
            'type': 'goal' if 'goal' in low else 'bug' if 'bug' in low else 'feature' if 'feature' in low else 'other',   # 0.18.1: bug wins
            'category': category(node['repository']['nameWithOwner'], low),   # 0.18: goal | core | app | bug (core vs app from the repo)
            'stage': item_group(node['repository']['nameWithOwner'], low, (node.get('state') or '').lower()) if 'goal' not in low else None,
            'state': (node.get('state') or '').lower(), 'state_reason': (node.get('stateReason') or '').lower() or None,
            'labels': labels, 'author': (node.get('author') or {}).get('login'),
            'created_at': node.get('createdAt'), 'updated_at': node.get('updatedAt'), 'closed_at': node.get('closedAt'),
            'comments': (node.get('comments') or {}).get('totalCount', 0),
            'tasks_done': sum(t['done'] for t in tasks), 'tasks_total': len(tasks),
            'open_prs': sum(1 for p in prs if p['state'] == 'open'), 'prs': prs}


def _repo(org, repo):
    """A repo filter: 'name' (inside the org) or 'owner/name'."""
    if not repo:
        return None
    if not isinstance(repo, str):
        raise Problem(400, 'repo must be a name.')
    parts = repo.strip().split('/')
    if len(parts) == 1 and NAME.match(parts[0]):
        return f'{org}/{parts[0]}'
    if len(parts) == 2 and all(NAME.match(p) for p in parts):
        return '/'.join(parts)
    raise Problem(400, 'repo must be name or owner/name.')


# GitHub search lags writes by up to a minute or two. Items this box just changed are kept for a
# short while and laid over search results, so a new goal never vanishes on the next refresh.
RECENT_SECONDS = 180
_recent = {}


def _remember(token, item):
    small = {k: v for k, v in item.items() if k not in ('body', 'tasks', 'comment_list', 'prs')}
    with LOCK:
        _recent[(_tid(token), item['id'])] = (time.time(), small)


def _merge_recent(token, items, f):
    now, t = time.time(), _tid(token)
    with LOCK:
        mine = [v for (k, _), (at, v) in list(_recent.items()) if now - at < RECENT_SECONDS]  # 0.17: shared, not per token
        for key in [k for k, (at, _) in _recent.items() if now - at >= RECENT_SECONDS]:
            _recent.pop(key, None)
    by_id = {i['id']: i for i in items}
    for r in mine:
        seen = by_id.get(r['id'])
        if seen and (seen.get('updated_at') or '') >= (r.get('updated_at') or ''):
            continue  # search already has this change (or a newer one)
        fits = (r['state'] == f['state'] and (f['type'] == 'all' or r['type'] == f['type'])
                and r['type'] in ('goal', 'bug')
                and (r['repo'].lower() == f['repo'].lower() if f['repo'] else r['repo'].lower().startswith(f['org'].lower() + '/')))
        if fits:
            by_id[r['id']] = r
        else:
            by_id.pop(r['id'], None)
    return sorted(by_id.values(), key=lambda i: i.get('updated_at') or '', reverse=True)


def board_list(person=None, args=None, agent=False):
    args = args or {}
    _, acct = account_for(person, agent)
    org = args.get('org') or settings()['org']
    if not isinstance(org, str) or not NAME.match(org):
        raise Problem(400, 'Use a GitHub organisation or user name.')
    kind, state = args.get('type') or 'all', args.get('state') or 'open'
    if kind not in ('all', 'goal', 'bug') or state not in ('open', 'closed'):
        raise Problem(400, 'Filters: type all|goal|bug, state open|closed.')
    repo = _repo(org, args.get('repo'))
    q = f"{'repo:' + repo if repo else 'org:' + org} is:issue is:{state} label:{ {'all': 'goal,bug', 'goal': 'goal', 'bug': 'bug'}[kind] } sort:updated-desc"
    if args.get('refresh'):
        _forget(acct['token'])
    found = ((graphql(acct['token'], SEARCH, {'q': unhide_q(q)}).get('data') or {}).get('search') or {})
    items = [shape(n) for n in found.get('nodes') or [] if n and n.get('number') and not hidden_repo(_node_repo(n))]
    items = _merge_recent(acct['token'], items, {'org': org, 'repo': repo, 'type': kind, 'state': state})
    for i in items:
        i.pop('prs', None)
    total = found.get('issueCount', len(items))
    return {'org': org, 'repo': repo, 'query': q, 'count': len(items), 'total': total, 'truncated': total > len(items),
            'goals': [i for i in items if i['type'] == 'goal'], 'bugs': [i for i in items if i['type'] == 'bug'],
            'fetched_at': int(time.time()), 'account': acct.get('login')}


def _target(args):
    repo, number = args.get('repo'), args.get('number')
    if isinstance(args.get('issue'), str):
        m = re.fullmatch(r'(?:https://github\.com/)?([\w.-]+)/([\w.-]+)(?:#|/issues/)(\d+)', args['issue'].strip())
        if not m:
            raise Problem(400, 'issue must look like owner/repo#123 or an issue URL.')
        repo, number = f'{m.group(1)}/{m.group(2)}', int(m.group(3))
    if isinstance(number, str) and number.isdigit():
        number = int(number)
    full = _repo(settings()['org'], repo)
    if not full or not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise Problem(400, 'Name the issue: repo and number (or issue "owner/repo#123").')
    return full, number


def _read_issue(acct, full, number, fresh):
    owner, name = full.split('/')
    node = ((graphql(acct['token'], ONE, {'o': owner, 'r': name, 'n': number}, cache=not fresh).get('data') or {}).get('repository') or {}).get('issue')
    if not node:
        raise Problem(404, f'{full}#{number} was not found, or your token cannot see it.')
    out = shape(node)
    out['body'] = node.get('body') or ''
    out['tasks'] = tasks_of(node.get('body'))
    out['comment_list'] = [{'author': (c.get('author') or {}).get('login'), 'body': c.get('body'), 'created_at': c.get('createdAt'), 'url': c.get('url')}
                           for c in (node.get('comments') or {}).get('nodes') or []]
    if fresh:
        _remember(acct['token'], out)
    return out


def board_get(person=None, args=None, agent=False, fresh=False):
    _, acct = account_for(person, agent)
    full, number = _target(args or {})
    return _read_issue(acct, full, number, fresh)


def _text(v, field, lo, hi):
    if not isinstance(v, str) or not lo <= len(v.strip()) <= hi:
        raise Problem(400, f'{field} must be {lo}–{hi} characters.')
    return v.strip()


def _issue_from_rest(full, issue, labels=None):
    """An item card from a REST issue (POST /repos/{full}/issues answer), when the GraphQL read-back failed."""
    names = labels if labels is not None else [l.get('name') for l in issue.get('labels') or [] if l.get('name')]
    node = {'repository': {'nameWithOwner': full}, 'number': issue['number'], 'title': issue.get('title'),
            'url': issue.get('html_url'), 'state': (issue.get('state') or 'open').upper(),
            'labels': {'nodes': [{'name': n} for n in names]}, 'author': {'login': (issue.get('user') or {}).get('login')},
            'createdAt': issue.get('created_at'), 'updatedAt': issue.get('updated_at'), 'body': issue.get('body') or '',
            'comments': {'totalCount': issue.get('comments') or 0}}
    out = shape(node)
    out['body'] = node['body']
    out['tasks'] = tasks_of(node['body'])
    out['comment_list'] = []
    return out


def _create(acct, full, title, body, label):
    """Open one issue with `label` (0.21: or a list of labels; the first one is the one the board needs)."""
    labels = list(label) if isinstance(label, (list, tuple)) else [label]
    label = labels[0]
    issue = gh(acct['token'], 'POST', f'/repos/{full}/issues', {'title': title, 'body': body, 'labels': labels})
    relabelled = False
    have = [l.get('name', '').lower() for l in issue.get('labels') or []]
    if any(x not in have for x in labels):
        # GitHub silently drops labels for people without write access; try once more, then say so.
        try:
            gh(acct['token'], 'POST', f"/repos/{full}/issues/{issue['number']}/labels", {'labels': labels})
            relabelled = True
        except Problem:
            pass
    try:
        out = _read_issue(acct, full, issue['number'], True)
    except Problem:
        # 0.30.1: the issue exists already. A GitHub hiccup on the read-back must not fail the request (the
        # person would see an error and file it again), so build the card from GitHub's answer to the create.
        out = _issue_from_rest(full, issue, labels if relabelled else None)
        _remember(acct['token'], out)
    out['labelled'] = all(x in [l.lower() for l in out['labels']] for x in labels)
    if not out['labelled']:
        out['warning'] = f'GitHub dropped the {label} label (you may not have write access to this repo), so it will not show on the board.'
    return out


def _owner_request(args, agent, what):
    if agent:
        r = args.get('owner_request')
        if not isinstance(r, str) or len(r.strip()) < 8:
            raise Problem(403, f'Agents may not {what} on their own. Only do it when a human asked; pass their words as owner_request.')


def add_goal(person, args, agent=False):
    _owner_request(args, agent, 'open goals')
    _, acct = account_for(person, agent)
    full = _repo(settings()['org'], args.get('repo'))
    if not full:
        raise Problem(400, 'Pick a repo for the goal.')
    title = _text(args.get('title'), 'Title', 3, 200)
    done = args.get('done') or []
    if isinstance(done, str):
        done = done.splitlines()
    if not isinstance(done, list) or not all(isinstance(d, str) for d in done):
        raise Problem(400, 'done must be a list of lines.')
    done = [d for d in (re.sub(r'^\s*(?:[-*+]\s*)?(?:\[[ xX]\]\s*)?', '', d).strip() for d in done) if d]
    if not 1 <= len(done) <= 20 or any(len(d) > 200 for d in done):
        raise Problem(400, 'Give the goal 1–20 done-list lines (up to 200 characters each).')
    intro = args.get('body') or ''
    if not isinstance(intro, str) or len(intro) > 4000:
        raise Problem(400, 'The description is too long (4000 characters max).')
    intro = intro.strip()
    body = (intro + '\n\n' if intro else '') + '## Done when\n\n' + '\n'.join(f'- [ ] {d}' for d in done) + '\n'
    return _create(acct, full, title, body, 'goal')


def report_bug(person, args, agent=False):
    _, acct = account_for(person, agent)
    full = _repo(settings()['org'], args.get('repo'))
    if not full:
        raise Problem(400, 'Pick a repo for the bug.')
    title = _text(args.get('title'), 'Title', 3, 200)
    body = args.get('body') or ''
    if not isinstance(body, str) or len(body) > 8000:
        raise Problem(400, 'The description is too long (8000 characters max).')
    return _create(acct, full, title, body.strip(), 'bug')


def report_feature(person, args, agent=False):
    """A feature to try (label feature): built switched off, tried in the Tracker, kept or dropped."""
    _, acct = account_for(person, agent)
    full = _repo(settings()['org'], args.get('repo'))
    if not full:
        raise Problem(400, 'Pick a repo for the feature.')
    title = _text(args.get('title'), 'Title', 3, 200)
    body = args.get('body') or ''
    if not isinstance(body, str) or len(body) > 8000:
        raise Problem(400, 'The description is too long (8000 characters max).')
    return _create(acct, full, title, body.strip(), 'feature')


def _current(acct, full, number):
    return gh(acct['token'], 'GET', f'/repos/{full}/issues/{number}', cache=False)


def set_state(person, args, state, agent=False):
    full, number = _target(args)
    _, acct = account_for(person, agent)
    cur = _current(acct, full, number)
    if agent and 'goal' in [l.get('name', '').lower() for l in cur.get('labels') or []]:
        raise Problem(403, 'Agents never close or reopen goals; only humans do, in the app.')
    if True:  # humans in the app, and agents for bugs (through the box's account, whose person must be the opener)
        # 0.12.1 (Miguel): a goal or bug is closed (marked Done) or reopened only by the person who opened it,
        # signed in with their own account (agents are refused in the human UI already). GitHub permissions
        # (Triage for agents, Write only for the builder, Owners Miguel and James) are the real enforcement.
        opener = ((cur.get('user') or {}).get('login') or '')
        if 'goal' in [l.get('name', '').lower() for l in cur.get('labels') or []]:
            # 0.17: only James closes or reopens a goal; closing needs every done-list item ticked, or `anyway`.
            if not goal_closer(acct.get('login') or ''):
                raise Problem(403, f'Only {goal_closers_text()} closes a goal.', 'not_goal_closer')
            tasks = tasks_of(cur.get('body'))
            if state == 'closed' and cur.get('state') != 'closed' and not (tasks and all(t['done'] for t in tasks)) and args.get('anyway') is not True:
                left = sum(1 for t in tasks if not t['done'])
                raise Problem(409, f'{left} done-list item{"" if left == 1 else "s"} not ticked yet. Close anyway?' if tasks
                              else 'This goal has no done-list. Close anyway?', 'not_all_done')
        elif not same_person(opener, acct.get('login') or ''):
            who = person_of(opener) or 'whoever opened it'
            kind = 'goal' if 'goal' in [l.get('name', '').lower() for l in cur.get('labels') or []] else 'bug'
            raise Problem(403, f"Only {who} can {'close' if state == 'closed' else 'reopen'} this {kind}.", 'not_opener')
    _owner_request(args, agent, 'close or reopen issues')
    if cur.get('state') != state:
        patch = {'state': state}
        if state == 'closed':
            reason = args.get('reason') or 'completed'
            if reason not in ('completed', 'not_planned'):
                raise Problem(400, 'reason must be completed or not_planned.')
            patch['state_reason'] = reason
        gh(acct['token'], 'PATCH', f'/repos/{full}/issues/{number}', patch)
    out = _read_issue(acct, full, number, True)
    if out['state'] != state:
        raise Problem(502, 'GitHub did not change the state. Refresh and try again.')
    return out


def set_task(person, args):
    _, acct = account_for(person)
    full, number = _target(args)
    index, checked, expect = args.get('index'), args.get('done'), args.get('text')
    if not isinstance(index, int) or isinstance(index, bool) or not isinstance(checked, bool):
        raise Problem(400, 'index must be a number and done true or false.')
    cur = _current(acct, full, number)
    if 'goal' in [l.get('name', '').lower() for l in cur.get('labels') or []]:   # 0.17: owners or James tick a goal
        ok, rule = human_rule('goal', (cur.get('user') or {}).get('login') or '',
                              [a.get('login') for a in cur.get('assignees') or [] if isinstance(a, dict)], acct.get('login') or '')
        if not ok:
            raise Problem(403, rule.replace(' review this goal.', ' tick this goal’s done-list.'), 'not_reviewer')
    lines = (cur.get('body') or '').split('\n')
    found = [i for i, l in enumerate(lines) if TASK.match(l.rstrip('\r'))]
    if not 0 <= index < len(found):
        raise Problem(409, 'The done-list changed on GitHub. Refresh and try again.')
    i = found[index]
    cr = '\r' if lines[i].endswith('\r') else ''
    m = TASK.match(lines[i].rstrip('\r'))
    if isinstance(expect, str) and m.group(4).strip() != expect.strip():
        raise Problem(409, 'That line changed on GitHub. Refresh and try again.')
    if (m.group(2) in 'xX') != checked:
        lines[i] = m.group(1) + ('x' if checked else ' ') + m.group(3) + m.group(4) + cr
        gh(acct['token'], 'PATCH', f'/repos/{full}/issues/{number}', {'body': '\n'.join(lines)})
    out = _read_issue(acct, full, number, True)
    if out['tasks'][index]['done'] != checked:
        raise Problem(502, 'GitHub did not save the tick. Refresh and try again.')
    return out


def add_comment(person, args, agent=False):
    _, acct = account_for(person, agent)
    full, number = _target(args)
    body = _text(args.get('body'), 'Comment', 1, 8000)
    c = gh(acct['token'], 'POST', f'/repos/{full}/issues/{number}/comments', {'body': body})
    got = gh(acct['token'], 'GET', f"/repos/{full}/issues/comments/{c['id']}", cache=False)
    if (got.get('body') or '').strip() != body:
        raise Problem(502, 'GitHub did not save the comment.')
    return {'repo': full, 'number': number, 'comment_url': got.get('html_url'), 'saved': True}


def repos(person):
    _, acct = account_for(person)
    org = settings()['org']
    try:
        rows = gh(acct['token'], 'GET', f'/orgs/{org}/repos?per_page=100&sort=pushed&type=all')
    except Problem as e:
        if e.code != 404:
            raise
        rows = gh(acct['token'], 'GET', f'/users/{org}/repos?per_page=100&sort=pushed')
        me = gh(acct['token'], 'GET', '/user')
        if (me.get('login') or '').lower() == org.lower():  # your own account: include private repos
            rows = [r for r in gh(acct['token'], 'GET', '/user/repos?per_page=100&affiliation=owner&sort=pushed') if r['owner']['login'].lower() == org.lower()]
    rows = [r for r in rows if not r.get('archived') and r.get('has_issues', True) and not hidden_repo(r.get('full_name'))]
    return {'org': org, 'repos': sorted([{'name': r['name'], 'full_name': r['full_name'], 'private': r.get('private', False),
                                          'can_write': bool((r.get('permissions') or {}).get('push'))} for r in rows], key=lambda r: r['name'].lower())}


ORG_REPOS_SECONDS = 3600
_org_repos = {}


APP_TOPIC = 'wizard-app'
_app_repos = {}   # full name (lower) -> has the wizard-app topic, filled by org_repos


def view_repos(token, org, view, base=None):
    """0.25 (Miguel): the Repo filter lists only repos that can hold this view's items. Core: the core repos; App
    features: Wizard app repos (topic wizard-app) that are not core; Bugs: every repo."""
    names = org_repos(token, org)
    if view == 'core':
        return [r for r in names if is_core(r, base)]
    if view == 'apps':
        return [r for r in names if not is_core(r, base) and _app_repos.get(r.lower())]
    return names


def org_repos(token, org):
    """Every non-archived repo of the org (names, sorted), cached an hour per token/org. Used by the Bugs
    view's Repo chips so repos with no open bugs still show (dimmed). [] when GitHub refuses."""
    key = (_tid(token), org.lower())
    with LOCK:
        hit = _org_repos.get(key)
    if hit and time.time() - hit[0] < ORG_REPOS_SECONDS:
        return hit[1]
    rows = []
    try:
        for page in range(1, 6):
            got = gh(token, 'GET', f'/orgs/{org}/repos?per_page=100&type=all&page={page}')
            got = got if isinstance(got, list) else []
            rows += got
            if len(got) < 100:
                break
    except Problem as e:
        if e.code != 404:
            return hit[1] if hit else []
        try:  # a personal account (the sandbox): its own repos
            rows = []
            for page in range(1, 6):
                got = gh(token, 'GET', f'/users/{org}/repos?per_page=100&type=owner&page={page}')
                got = got if isinstance(got, list) else []
                rows += got
                if len(got) < 100:
                    break
        except Problem:
            return hit[1] if hit else []
    names = sorted({r['full_name'] for r in rows if isinstance(r, dict) and r.get('full_name') and not r.get('archived')
                    and not hidden_repo(r['full_name'])}, key=str.lower)
    with LOCK:
        _org_repos[key] = (time.time(), names)
        # 0.25: remember which repos are Wizard apps (topic wizard-app) for the App features Repo filter
        _app_repos.update({r['full_name'].lower(): APP_TOPIC in (r.get('topics') or []) for r in rows
                           if isinstance(r, dict) and r.get('full_name')})
    return names


def boxes(person):
    """Which wizard-ux this box runs vs main. The box's own version file is outside this
    app's container, so that half stays 'coming soon' rather than asking for host access."""
    out = {'box': {'available': False, 'note': 'Coming soon: the app cannot read this box’s version yet.'}, 'main': None}
    try:
        _, acct = account_for(person)
        c = gh(acct['token'], 'GET', '/repos/humanitylabs-org/wizard-ux/commits/main')
        out['main'] = {'sha': (c.get('sha') or '')[:7], 'message': ((c.get('commit') or {}).get('message') or '').split('\n')[0][:120],
                       'date': ((c.get('commit') or {}).get('committer') or {}).get('date'), 'url': c.get('html_url')}
    except Problem as e:
        out['main_error'] = e.message
    return out


_avatars = {}


def avatar(person):
    """The connected account's picture, fetched by the box so the view loads only from itself."""
    url = ((accounts()['people'].get(person_key(person))) or {}).get('avatar_url') or ''
    if not url.startswith('https://avatars.githubusercontent.com/'):
        raise Problem(404, 'No picture.')
    hit = _avatars.get(url)
    if hit and time.time() - hit[0] < 3600:
        return hit[1], hit[2]
    try:
        with urllib.request.urlopen(urllib.request.Request(url + ('&' if '?' in url else '?') + 's=96', headers={'User-Agent': USER_AGENT}), timeout=10) as r:
            data, kind = r.read(512 * 1024), r.headers.get('Content-Type', '')
    except (urllib.error.URLError, OSError):
        raise Problem(502, 'No picture.') from None
    if kind not in ('image/png', 'image/jpeg', 'image/gif', 'image/webp'):
        raise Problem(502, 'No picture.')
    _avatars[url] = (time.time(), data, kind)
    return data, kind



# ---------- the three tabs (Goals, To test, Issues) ----------

VIEW_FIELDS = (' number title url body createdAt updatedAt author { login } assignees(first: 10) { nodes { login } }'
               ' repository { nameWithOwner } labels(first: 20) { nodes { name } }'
               ' comments(last: 20) { nodes { author { login } body createdAt } }'
               ' timelineItems(itemTypes: [CROSS_REFERENCED_EVENT, CONNECTED_EVENT], last: 60) { nodes {'
               '  ... on CrossReferencedEvent { source { __typename'
               '    ... on Issue { number url state repository { nameWithOwner } }'
               '    ... on PullRequest { number url state title merged mergeCommit { oid } baseRefName repository { nameWithOwner } } } }'
               '  ... on ConnectedEvent { subject { __typename ... on PullRequest { number url state title merged mergeCommit { oid } baseRefName repository { nameWithOwner } } } }'
               ' } }')
VIEW = 'query($q: String!) { search(query: $q, type: ISSUE, first: 80) { issueCount nodes { ... on Issue {' + VIEW_FIELDS + ' } } } }'
# 0.21: one issue with the view's fields (a just-filed report shown at once; was referenced but never defined in 0.20)
VIEW_ONE = 'query($o: String!, $r: String!, $n: Int!) { repository(owner: $o, name: $r) { issue(number: $n) {' + VIEW_FIELDS + ' } } }'
MAX_ITEMS = 300
# Lists page through search: light rows (no timeline or comments) 100 at a time for Bugs; the heavy
# fields (linked issues for goal progress, the builder's last comment for Ready to test) only where shown.
LIGHT_FIELDS = VIEW_FIELDS.split(' comments(last')[0] + ' comments { totalCount }'
HEAVY_FIELDS = VIEW_FIELDS.replace('comments(last: 20) { nodes', 'comments(last: 10) { totalCount nodes')
_PAGE = 'search(query: $q, type: ISSUE, first: %d, after: $after) { issueCount pageInfo { hasNextPage endCursor } nodes { ... on Issue {%s } } }'
PAGES = {heavy: 'query($q: String!, $after: String) { ' + _PAGE % (size, fields) + ' }'
         for heavy, size, fields in ((False, 100, LIGHT_FIELDS), (True, 40, HEAVY_FIELDS))}
# Bottleneck stats: every issue of one kind (open and closed) with its label/close history, 100 a page.
STATS_PAGE = ('query($q: String!, $after: String) { search(query: $q, type: ISSUE, first: 100, after: $after) {'
              ' issueCount pageInfo { hasNextPage endCursor } nodes { ... on Issue { number state createdAt closedAt'
              ' author { login } repository { nameWithOwner }'
              ' timelineItems(itemTypes: [LABELED_EVENT, UNLABELED_EVENT, CLOSED_EVENT, REOPENED_EVENT], first: 100) { nodes {'
              ' __typename ... on LabeledEvent { createdAt label { name } } ... on UnlabeledEvent { createdAt label { name } }'
              ' ... on ClosedEvent { createdAt } ... on ReopenedEvent { createdAt } } } } } } }')
STATS_MAX = 600
_tested = {}   # (token id, issue id) -> time: tested here, hidden from To test while search catches up
_made = {}     # (token id, issue id) -> (time, tab, item): created here, shown while search catches up


def _plain(text, limit):
    """Markdown-ish text to one short plain paragraph."""
    t = re.sub(r'```.*?```', ' ', text or '', flags=re.S)
    t = re.sub(r'<!--.*?-->', ' ', t, flags=re.S)
    t = re.sub(r'!\[[^\]]*\]\([^)]*\)', ' ', t)
    t = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', t)
    t = re.sub(r'^\s{0,3}(#{1,6}|>|[-*+]\s+\[[ xX]\]|[-*+]|\d+\.)\s*', '', t, flags=re.M)
    t = re.sub(r'[*_`]{1,3}', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t if len(t) <= limit else t[:limit - 1].rsplit(' ', 1)[0] + '…'


_LINK_ONLY = re.compile(r'(part of|closes|fixes|refs?|related:?)\s+\S*#\d+\.?', re.I)


def describe(body, limit=400):
    """The first real paragraph of an issue body as plain text: stops at a line that is just ---,
    skips headings, bold-only label lines ("**What happens**"), comments and link-only lines."""
    text = re.sub(r'<!--.*?-->', ' ', body or '', flags=re.S)
    text = re.sub(r'```.*?```', '\n\n', text, flags=re.S)
    lines = []
    for line in text.replace('\r', '').split('\n'):
        if re.fullmatch(r'\s*-{3,}\s*', line):
            break
        if re.match(r'^\s{0,3}#{1,6}\s', line) or re.fullmatch(r'\s*(\*\*|__)[^*_]{1,80}(\*\*|__):?\s*', line):
            lines.append('')  # a heading ends a paragraph and is not part of one
            continue
        lines.append(line)
    for para in re.split(r'\n\s*\n', '\n'.join(lines)):
        plain = _plain(para, limit)
        if plain and not _LINK_ONLY.fullmatch(plain) and re.search(r'\w', plain):
            return plain
    return ''


def _refs(node):
    """Issues and PRs that link to or mention this issue (deduplicated, not itself)."""
    me, seen = node.get('url'), {}
    for t in (node.get('timelineItems') or {}).get('nodes') or []:
        x = (t or {}).get('source') or (t or {}).get('subject')
        if x and x.get('url') and x['url'] != me:
            seen[x['url']] = {'kind': 'pr' if x.get('__typename') == 'PullRequest' else 'issue', 'number': x.get('number'),
                              'url': x['url'], 'state': (x.get('state') or '').lower(), 'title': x.get('title'),
                              'repo': (x.get('repository') or {}).get('nameWithOwner')}
    return list(seen.values())


def _card(node, tab):
    labels = [l['name'].lower() for l in (node.get('labels') or {}).get('nodes') or []]
    author = (node.get('author') or {}).get('login')
    assignee = next((a.get('login') for a in (node.get('assignees') or {}).get('nodes') or [] if a), None)
    repo = node['repository']['nameWithOwner']
    out = {'id': f"{repo}#{node['number']}", 'repo': repo, 'number': node['number'], 'title': node.get('title'),
           'url': node.get('url'), 'author': author, 'updated_at': node.get('updatedAt'),
           'kind': 'goal' if 'goal' in labels else 'bug' if 'bug' in labels else 'issue',
           'waiting': 'ready-to-test' in labels, 'approved': 'approved' in labels,
           'assignees': [a.get('login') for a in (node.get('assignees') or {}).get('nodes') or [] if a and a.get('login')],
           'comments': ((node.get('comments') or {}).get('totalCount')), 'desc': describe(node.get('body')),
           'from_feedback': feedback_inbox.FROM_LABEL in labels}   # 0.26.5: a 'From Feedback app' chip
    if out['comments'] is None:
        out['comments'] = len((node.get('comments') or {}).get('nodes') or [])
    refs = _refs(node)
    if tab == 'goals':
        linked = [r for r in refs if r['kind'] == 'issue']  # issues that link to or mention the goal
        done = sum(1 for r in linked if r['state'] in ('closed', 'merged'))
        tasks = tasks_of(node.get('body'))
        if linked:
            prog = {'done': done, 'total': len(linked), 'of': 'linked issues'}
        elif tasks:
            prog = {'done': sum(t['done'] for t in tasks), 'total': len(tasks), 'of': 'done-list items'}
        else:
            prog = {'done': 0, 'total': 0, 'of': ''}
        status = ('ready' if 'ready-to-test' in labels else 'done' if prog['total'] and prog['done'] == prog['total']
                  else 'doing' if prog['done'] or linked else 'new')
        out.update(owner=assignee or author, progress=prog, status=status)
    elif tab == 'test':
        said = [c for c in (node.get('comments') or {}).get('nodes') or []
                if ((c.get('author') or {}).get('login') or '').lower() in BUILDERS]
        out['check'] = re.sub(r'^(ready (to|for) test(ing)?)[.:!\s—-]*', '', _plain(said[-1]['body'], 320), flags=re.I) if said else ''
        out['check_from'] = 'builder' if said else ''
        out['switch'], out['switch_how'] = switch_info([c.get('body') for c in said], node.get('body'))
        out['check_at'], out['shots'] = bug_check_info([c.get('body') for c in said])   # 0.26: bugs' optional check
        out['fix'] = bug_handover([c.get('body') for c in said])   # 0.26.1: what changed / how to check, plain words
        # 0.24.5: an app switch named the catalog way ('Switch: <app>:<id>') counts from any comment (the owner's agent
        # links an existing app switch to its issue); the newest such line wins over the builder's words.
        if not out['switch']:
            named = app_switch_line([c.get('body') for c in (node.get('comments') or {}).get('nodes') or []])
            if named:
                out['switch_how'] = named
        out['switch_place'] = None if out['switch'] else switch_place(out['switch_how'], [c.get('body') for c in said] + [node.get('body')], repo)
        # 0.27 (Miguel, wizard-notes#18): an item that says it has no Try it switch (an install-time setting, 'Switch:
        # none' after the switch was removed) gets no toggle, only its 'How to check' line. A later catalog-form
        # 'Switch: <app>:<id>' line still wins.
        if no_switch([c.get('body') for c in (node.get('comments') or {}).get('nodes') or []], node.get('body')):
            out.update(switch=None, switch_how=None, switch_place=None, no_switch=True)
        if not said:  # first real line of the description (not a "Part of #13" link line)
            lines = [_plain(x, 220) for x in re.split(r'\n\s*\n|\r?\n', node.get('body') or '')]
            out['check'] = next((x for x in lines if x and not re.fullmatch(r'(part of|closes|fixes|refs?)\s+\S*#\d+\.?', x, re.I)), '')
        prs = [r for r in refs if r['kind'] == 'pr']
        pr = next((r for r in prs if r['state'] == 'open'), None) or next((r for r in prs if r['state'] == 'merged'), None) or (prs[0] if prs else None)
        out['pr'] = {'url': pr['url'], 'number': pr['number'], 'repo': pr['repo']} if pr else None
        out['delivers'], out['open_prs'] = delivered(node)          # 0.16: what to look for on this Wizard
        out['steps'] = test_steps([(((c.get('author') or {}).get('login') or ''), c.get('body') or '')
                                   for c in (node.get('comments') or {}).get('nodes') or []])
    return out


# 0.14 Try it: the builder names the change's feature switch in its "What to check" hand-over comment with one line
# `Switch: <author>/<id>` (the id in the shell's tweaks catalog). The newest builder comment that has one wins; the
# issue body is the fallback (a person may add it there). None: the change has no switch.
SWITCH_LINE = re.compile(r'^[ \t>*_`-]*switch[*_`]*\s*:[\s*_`]*([a-z0-9][a-z0-9-]{0,39}/[a-z0-9][a-z0-9-]{0,63})[*_`]*[ \t]*(?:\([^\n]*\))?\s*\.?\s*$',
                         re.I | re.M)


# 0.15: a `Switch:` line that is not a shell tweak id (a device setting, an env knob) still marks a switched change; the
# Tracker cannot flip it, so it shows the line's words as how to turn it on.
ANY_SWITCH_LINE = re.compile(r'^[ \t>*_`-]*switch[*_`]*\s*:[*_`]*[ \t]*(\S.*?)\s*$', re.I | re.M)
NO_SWITCH = re.compile(r'^(none|no|n/?a|nothing|no switch|-+)(\W|$)', re.I)  # 'Switch: none. It ships live.'


def _switch_how(text):
    t = re.sub(r'\([^)]*\)', '', text)            # drop asides: '(off by default)', '(device setting, ...)'
    t = re.sub(r'[`*]+|(?<![A-Za-z0-9])_+|_+(?![A-Za-z0-9])', '', t).strip(' .;:-')  # markdown marks, not WORD_WORD
    t = re.sub(r'\s+', ' ', t).replace(' > ', ' › ')
    return None if not t or NO_SWITCH.match(t) else t[:120]


def switch_info(builder_bodies, issue_body=None):
    """(tweak id or None, how-to text or None). A shell tweak id anywhere wins (newest builder comment first, then the
    body); otherwise the newest other Switch line, as words. 'Switch: none' lines count as no switch."""
    bodies = [b for b in [*reversed([b for b in builder_bodies or [] if isinstance(b, str)]), issue_body] if isinstance(b, str)]
    for body in bodies:
        m = SWITCH_LINE.findall(body)
        if m:
            return m[-1].lower(), None
    for body in bodies:
        hows = [h for h in (_switch_how(x) for x in ANY_SWITCH_LINE.findall(body)) if h]
        if hows:
            return None, hows[-1]
    return None, None


MAC_CMD = re.compile(r'defaults\s+write\s+[A-Za-z0-9_.-]+\s+[A-Za-z0-9_.-]+\s+-bool\s+(?:YES|true|1)\b')
ENV_HOW = re.compile(r'^(?:env\s+)?[A-Z][A-Z0-9_]{2,}=\S+$|^[a-z0-9-]+/[A-Z][A-Z0-9_]{2,}\b|^env\b')


def switch_place(how, bodies, repo):
    """0.17: where to flip a switch the Tracker can't flip itself. {kind mac|app|box|other, text, cmd?, repo?}:
    a Mac user default (with the exact Terminal command), a setting inside one app (the page opens that app), a box
    knob for whoever runs the box, or the words as written."""
    if not how:
        return None
    texts = [b for b in bodies or [] if isinstance(b, str)]
    cmd = next((m.group(0) for b in reversed(texts) for m in [MAC_CMD.search(b)] if m), None)
    name = (repo or '').split('/')[-1].lower()
    word = re.split(r'[\s(]', how, maxsplit=1)[0]
    if cmd or name in MAC_REPOS or 'mac user default' in ' '.join(texts).lower():
        if not cmd and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{2,60}', word):
            cmd = f'defaults write org.humanitylabs.wizard.mvp {word} -bool YES'
        return {'kind': 'mac', 'text': f'On your Mac: {word}. Run this in Terminal, then quit and reopen Wizard.', 'cmd': cmd}
    if ENV_HOW.search(how):
        return {'kind': 'box', 'text': f'On the Wizard box (whoever runs it): {re.split(r"\s*\.\s+", how, maxsplit=1)[0].strip(" .")}'}
    if 'settings' in how.lower() and name not in OFF_BOX and name not in ('wizard-ux', 'wizard-app-manager', 'wizard-agent-service'):
        return {'kind': 'app', 'text': how if how.lower().startswith('settings') else how, 'repo': repo}
    if how.lower().startswith('settings'):
        return {'kind': 'settings', 'text': f'In Wizard: {how}.'}
    return {'kind': 'other', 'text': how}


APP_SWITCH_LINE = re.compile(r'^[ \t>*_`-]*switch[*_`]*\s*:[\s*_`]*([a-z0-9][a-z0-9-]{0,63}:[a-z0-9][a-z0-9-]{0,63})[*_`]*[ \t]*(?:\([^\n]*\))?\s*\.?\s*$',
                             re.I | re.M)


def app_switch_line(bodies):
    """0.24.5: the newest 'Switch: <app>:<switch>' line in any of these comment bodies (oldest first), lower-cased."""
    for body in reversed([b for b in bodies or [] if isinstance(b, str)]):
        m = APP_SWITCH_LINE.findall(body)
        if m:
            return m[-1].lower()
    return None


NO_SWITCH_SAID = re.compile(r"\bno\s+(?:tracker\s+)?try[- ]it\s+(?:switch|toggle)|\bno\s+(?:feature\s+|tracker\s+)?(?:switch|toggle)\s+for\s+(?:this|it)\b"
                            r"|\bnot\s+a\s+(?:tracker\s+)?(?:try[- ]it\s+)?toggle\b", re.I)


def no_switch(bodies, issue_body=None):
    """0.27 (wizard-notes#18): do the newest words say there is no Try it switch ('No Try it switch for this one', 'no
    Tracker Try-it toggle', 'not a Tracker toggle')? Comments oldest first; a shell tweak or 'Switch: <app>:<id>' line
    in a later comment means a switch was linked since."""
    texts = [issue_body] + list(bodies or [])
    for body in reversed([b for b in texts if isinstance(b, str)]):
        if APP_SWITCH_LINE.search(body) or SWITCH_LINE.search(body):
            return False
        if NO_SWITCH_SAID.search(body):
            return True
    return False


def switch_of(builder_bodies, issue_body=None):
    return switch_info(builder_bodies, issue_body)[0]


# ---------- 0.16 'Is it on my Wizard?' ----------
# A Human review item is testable on a Wizard only once the box runs a build that contains the merged PR(s) that
# delivered it. The page (on the box's own shell origin) reads what the box runs: the WizardOS release
# (store-install/_updates) and each app's /<id>/healthz version. This server maps those to commits (release manifest
# in wizard-app-manager/releases, the Store index listing, the app recipe's pinned `sha-<commit>` image) and asks
# GitHub's compare API whether that build contains the merge commit (identical/ahead = contains). Immutable answers are
# cached for good, pointers and listings ~10 min, verdicts 5 min: the list never waits for any of it.
SHA40 = re.compile(r'^[0-9a-f]{40}$')
REPO_FULL = re.compile(r'^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$')
APP_ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,60}$')
VERSION_RE = re.compile(r'^[0-9A-Za-z][0-9A-Za-z.+_-]{0,60}$')
RELEASES_REPO = os.environ.get('WIZARD_RELEASES_REPO', 'humanitylabs-org/wizard-app-manager')
STORE_INDEX = os.environ.get('WIZARD_STORE_INDEX', 'https://apps.aiwizards.com').rstrip('/')
CORE_REPOS = ('wizard-os', 'wizard-ux', 'wizard-app-manager', 'wizard-agent-service', 'try-wizard')
MAC_REPOS = {'try-wizard'}                                   # the Mac app: ships as a Mac build, not on the box
OFF_BOX = {'wizard-os', 'wizard-app-index', 'wizard-issue-media', '.github'}   # specs, tools, websites: never on a box
MAC_FIX = 'Needs the Mac app update'
FIX_ORDER = ('Not released yet', MAC_FIX, 'Switch to For testers in Settings', 'Update WizardOS in Settings')
ON_TEXT, OFF_TEXT, UNKNOWN_TEXT = 'On your Wizard ✓', 'Not on your Wizard yet', 'Can’t tell if this is on your Wizard'
_forever = {}     # immutable lookups: compare(sha, sha), manifests, version -> commit, recipe -> image commit
_timed = {}       # key -> (time, value): release pointers, Store listings, Mac releases (10 min)
_verdicts = {}    # (token id, item id, box key) -> (time, verdict)
TIMED_SECONDS, VERDICT_SECONDS = 600, 300
_NONE = object()


def delivered(node):
    """([{repo, number, sha, url}] merged PRs that link to this issue, count of open ones)."""
    seen, open_prs = {}, 0
    for t in (node.get('timelineItems') or {}).get('nodes') or []:
        x = (t or {}).get('source') or (t or {}).get('subject')
        if not x or x.get('__typename') != 'PullRequest':
            continue
        repo = (x.get('repository') or {}).get('nameWithOwner')
        sha = ((x.get('mergeCommit') or {}).get('oid') or '').lower()
        if x.get('merged') and repo and (SHA40.match(sha) or not sha):
            # 0.17: a token that can't read a repo's commits gets mergeCommit null; keep the PR (sha '') so a Mac change
            # still counts (it needs the Mac app) and anything else says it can't tell, instead of vanishing
            seen[(repo.lower(), x.get('number'))] = {'repo': repo, 'number': x.get('number'), 'sha': sha if SHA40.match(sha) else '', 'url': x.get('url')}
        elif (x.get('state') or '').upper() == 'OPEN':
            open_prs += 1
    return list(seen.values())[:12], open_prs


_STEPS = re.compile(r'(?:^|\n|[.!?)]\s+)[ \t>*_]*(?:human(?:\s+(?:test|review|check))?|for the (?:person|human|tester) (?:checking|testing|reviewing)|'
                    r'to test(?: it)?|how to test|for the tester|test steps)[*_]*(?:\s*\([^)\n]*\))?[*_]*\s*[:—][*_]*\s*(.+?)(?=\n\s*\n|\Z)', re.I | re.S)


def test_steps(rows, limit=280):
    """What a person does to test it, from the newest 'AI review: pass' note ('Human test: …', 'For the person
    checking: …', 'Human: …'). One plain line; '' when the note has none."""
    for _, body in reversed(rows or []):
        if not re.match(r'^\s*AI review:\s*pass', body or '', re.I):
            continue
        m = _STEPS.search(body)
        if not m:
            return ''
        t = re.sub(r'(?m)^\s*(\d+[.)]|[-*+])\s+', '', m.group(1))
        t = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', t)
        t = re.sub(r'`+|\*{1,3}|(?<![A-Za-z0-9])_{1,2}|_{1,2}(?![A-Za-z0-9])', '', t)   # code marks, not snake_case
        t = re.sub(r'\s+', ' ', t).strip()
        return t if len(t) <= limit else t[:limit - 1].rsplit(' ', 1)[0] + '…'
    return ''


def _timed_get(key, fn, seconds=TIMED_SECONDS):
    with LOCK:
        hit = _timed.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    try:
        v = fn()
    except Problem:
        v = None
    with LOCK:
        _timed[key] = (time.time(), v)
    return v


def _forever_get(key, fn):
    with LOCK:
        if key in _forever:
            return _forever[key]
    try:
        v = fn()
    except Problem as e:
        if e.code in (401, 429, 502):
            return None          # transient: ask again next time
        v = None                 # 404/403: the commit or file isn't there; that won't change
    with LOCK:
        _forever[key] = v
        if len(_forever) > 4000:
            for k in list(_forever)[:500]:
                _forever.pop(k, None)
    return v


def _file_at(token, repo, path, ref):
    got = gh(token, 'GET', f'/repos/{repo}/contents/{path}?ref={urllib.parse.quote(ref, safe="")}')
    if not isinstance(got, dict) or got.get('encoding') != 'base64':
        return None
    return base64.b64decode(got.get('content') or '').decode('utf-8', 'replace')


def contains(token, repo, sha, build):
    """True when commit `build` of `repo` contains `sha`; None when GitHub can't say."""
    if not sha or not build:
        return None
    if sha == build:
        return True
    def ask():
        got = gh(token, 'GET', f'/repos/{repo}/compare/{sha}...{build}?per_page=1')
        st = (got or {}).get('status')
        return st in ('identical', 'ahead') if st else None
    return (_forever_get(('cmp', repo.lower(), sha, build), ask) if SHA40.match(build)
            else _timed_get(('cmp', repo.lower(), sha, build), ask))


def release_manifest(token, version):
    if not version or not VERSION_RE.match(version):
        return None
    def get():
        raw = _file_at(token, RELEASES_REPO, f'releases/{version}.json', 'main')
        return json.loads(raw) if raw else None
    return _forever_get(('rel', version), get)


def release_pointer(token, name):
    def get():
        raw = (_file_at(token, RELEASES_REPO, f'releases/{name}', 'main') or '').strip()
        return raw if VERSION_RE.match(raw) else None
    return _timed_get(('ptr', name), get)


def release_commit(manifest, repo):
    """The commit a release pins for a box component (components: App Manager, Agent Service, Wizard UX)."""
    for c in ((manifest or {}).get('components') or {}).values():
        if isinstance(c, dict) and str(c.get('repo') or '').lower() == repo.lower() and SHA40.match(str(c.get('commit') or '')):
            return c['commit']
    return None


def store_listing(repo):
    def get():
        try:
            req = urllib.request.Request(f'{STORE_INDEX}/v1/apps/{repo}', headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read(1024 * 1024) or b'{}')
        except (OSError, ValueError):
            return None
        if not isinstance(d, dict) or not d.get('id'):
            return None
        commit = str(((d.get('install') or {}).get('commit')) or '').lower()
        return {'id': d['id'], 'name': d.get('name') or d['id'], 'version': str(d.get('version') or ''),
                'commit': commit if SHA40.match(commit) else None}
    return _timed_get(('store', repo.lower()), get)


def version_commit(token, repo, version):
    """The recipe commit whose runtipi/config.json carries `version` (newest first, last 30 changes)."""
    def get():
        rows = gh(token, 'GET', f'/repos/{repo}/commits?path=runtipi/config.json&per_page=30')
        for row in rows if isinstance(rows, list) else []:
            sha = (row or {}).get('sha') or ''
            raw = _forever_get(('file', repo.lower(), 'runtipi/config.json', sha), lambda: _file_at(token, repo, 'runtipi/config.json', sha))
            try:
                if raw and str(json.loads(raw).get('version')) == version:
                    return sha
            except ValueError:
                continue
        return None
    return _forever_get(('ver', repo.lower(), version), get)


def code_commit(token, repo, recipe):
    """The code commit a recipe runs: its compose pins the repo's own image as `sha-<40 hex>`; else the recipe itself."""
    if not recipe:
        return None
    def get():
        raw = _file_at(token, repo, 'runtipi/docker-compose.yml', recipe) or ''
        m = re.search(r'ghcr\.io/' + re.escape(repo.lower()) + r':sha-([0-9a-f]{40})', raw.lower())
        return m.group(1) if m else recipe
    return _forever_get(('img', repo.lower(), recipe), get) or recipe


def mac_release(token, repo):
    def get():
        rows = gh(token, 'GET', f'/repos/{repo}/releases?per_page=10')
        rows = [r for r in rows if isinstance(r, dict) and not r.get('draft') and r.get('published_at') and r.get('tag_name')] if isinstance(rows, list) else []
        rows.sort(key=lambda r: r['published_at'], reverse=True)
        return rows[0]['tag_name'] if rows else None
    return _timed_get(('mac', repo.lower()), get)


RECIPE_ONLY = re.compile(r'^(runtipi/|\.github/|wizard\.md$|[^/]+\.md$)')


def recipe_only(token, repo, code, sha):
    """0.33.4: True when every file that changed from the image's code commit to `sha` is recipe or notes (a Store
    recipe PR that pins the image, like Messenger's 'pin sha-…' releases): the code at `sha` is the image's code."""
    def ask():
        got = gh(token, 'GET', f'/repos/{repo}/compare/{code}...{sha}?per_page=100')
        if not isinstance(got, dict) or got.get('status') not in ('ahead', 'identical'):
            return None
        files = got.get('files')
        if not isinstance(files, list) or len(files) >= 300:
            return None
        return all(RECIPE_ONLY.match(str((f or {}).get('filename') or '')) for f in files)
    return _forever_get(('recipe-only', repo.lower(), code, sha), ask)


def in_build(token, repo, sha, recipe):
    """Is merged PR `sha` in what recipe commit `recipe` installs? Its image's code commit holds it, or the PR changed
    only the recipe and the recipe has it (a recipe PR's own merge commit is never inside the image it pins)."""
    if not recipe:
        return None
    code = code_commit(token, repo, recipe)
    got = contains(token, repo, sha, code)
    if got is False and code and code != recipe and contains(token, repo, sha, recipe):
        return bool(recipe_only(token, repo, code, sha))
    return got


def where(token, d, box):
    """One merged PR on this Wizard: ('yes'|'no'|'unknown'|'na', fix text or None, app id the box should report)."""
    repo, sha = d['repo'], d['sha']
    name = repo.split('/')[-1].lower()
    if name in OFF_BOX:
        return 'na', None, None
    if name in MAC_REPOS:
        # 0.17: a Mac app change is only on your Wizard when the Mac app you look from is known to contain it. The page
        # sends its Mac app version when it runs inside one; otherwise (browser, phone, unknown) it needs the Mac app.
        mac = box.get('mac')
        if mac and sha:
            got = None
            for tag in (f'mac-mvp-{mac}', f'v{mac}', mac):   # try-wizard tags Mac releases mac-mvp-<version>
                try:
                    got = contains(token, repo, sha, tag)
                except Problem:   # no such tag
                    got = None
                if got is not None:
                    break
            if got:
                return 'yes', None, None
        return 'no', MAC_FIX, None
    if not sha:
        return 'unknown', None, None
    release = box.get('release')
    manifest = release_manifest(token, release) if release else None
    pinned = release_commit(manifest, repo)
    if pinned or name in ('wizard-ux', 'wizard-agent-service', 'wizard-app-manager'):
        if not pinned:
            return 'unknown', None, None
        have = contains(token, repo, sha, pinned)
        if have is None:
            return 'unknown', None, None
        if have:
            return 'yes', None, None
        chan = 'latest' if box.get('channel') == 'latest' else 'stable'
        def in_release(v):
            return bool(v) and v != release and bool(contains(token, repo, sha, release_commit(release_manifest(token, v), repo)))
        if in_release(release_pointer(token, chan)):
            return 'no', 'Update WizardOS in Settings', None
        if chan != 'latest' and in_release(release_pointer(token, 'latest')):
            return 'no', 'Switch to For testers in Settings', None
        return 'no', 'Not released yet', None
    app = store_listing(repo)
    if not app:
        return 'unknown', None, None
    ver = (box.get('apps') or {}).get(app['id'])
    if not ver:
        return 'unknown', None, app['id']
    # 0.33.3: the recipe that set the installed version, not the listing's newest commit: an app whose recipe pins its
    # image by tag (Wizard Notes) keeps the same version while main moves on, so the listing commit over-claims.
    recipe = version_commit(token, repo, ver) or (app['commit'] if ver == app['version'] else None)
    have = in_build(token, repo, sha, recipe)
    if have is None:
        return 'unknown', None, app['id']
    if have:
        return 'yes', None, app['id']
    # 0.33.6: the Store's newest version is the one installed, so no Update can bring it (its listing commit is main)
    listed = app.get('version') != ver and in_build(token, repo, sha, app['commit'])
    return 'no', (f"Update {app['name']}" if listed else 'Not released yet'), app['id']


def verdict(token, delivers, open_prs, box):
    """Combine every merged PR of one item: any missing -> not on this Wizard (the most blocking fix first)."""
    parts = [(d, *where(token, d, box)) for d in delivers]
    apps = sorted({a for *_, a in parts if a})
    shown = [{'repo': d['repo'], 'number': d['number'], 'state': st, 'fix': fix} for d, st, fix, _ in parts]
    nos = [fix for _, st, fix, _ in parts if st == 'no']
    if nos or (not delivers and open_prs):
        fix = min(nos or ['Not released yet'], key=lambda f: FIX_ORDER.index(f) if f in FIX_ORDER else len(FIX_ORDER))
        return {'state': 'no', 'text': OFF_TEXT, 'fix': fix, 'parts': shown, 'apps': apps}
    states = {st for _, st, _, _ in parts if st != 'na'}
    if states == {'yes'}:
        return {'state': 'yes', 'text': ON_TEXT, 'fix': None, 'parts': shown, 'apps': apps}
    return {'state': 'unknown', 'text': UNKNOWN_TEXT, 'fix': None, 'parts': shown, 'apps': apps}


# ---------- 0.18 Shipping -> Done: is the approved change in a release that reaches everyone? ----------
# After human approval an item is Shipping until every merged PR that delivered it is in a released build; then Done.
# Core box components (pinned in a WizardOS release manifest): merged -> 'Waiting for release' -> 'In For testers <v>'
# (in the `latest` release) -> Done 'Shipped in <v>' (in the `stable` release, For everyone). The Mac app (try-wizard):
# 'Waiting for Mac app build' until a published Mac release contains it. Apps: 'Waiting for <App> release' until the
# Store's latest release contains it. Specs and tools (wizard-os, ...) ship on merge. The first version seen is kept
# (DATA_DIR/private/shipped.json), so 'Shipped in 0.3.134' does not move when a later release comes out.
SHIP_SECONDS = 600
SHIP_STALE_OK, WARM_SECONDS = 3600, 180
_WARMING = threading.local()
_ship = {}   # (item id, delivers key) -> (time, verdict)
SHIP_RANK = {'waiting': 0, 'mac': 1, 'app': 1, 'testers': 2, 'unknown': 3, 'done': 4}


def _vtag(tag):
    return re.sub(r'^(mac-mvp-|v)', '', tag or '')


def ship_one(token, d, base=None):
    """One merged PR: (wait key, text, version or None)."""
    repo, sha = d['repo'], d.get('sha') or ''
    name = repo.split('/')[-1].lower()
    if name in OFF_BOX:
        return 'done', 'Shipped on merge (no release needed)', None
    if name in MAC_REPOS:
        tag = mac_release(token, repo)
        if tag and sha:
            try:
                have = contains(token, repo, sha, tag)
            except Problem:
                have = None
            if have:
                return 'done', f'Shipped in Mac app {_vtag(tag)}', 'mac ' + _vtag(tag)
        return 'mac', 'Waiting for Mac app build', None
    stable, latest = release_pointer(token, 'stable'), release_pointer(token, 'latest')
    pins = {v: release_commit(release_manifest(token, v), repo) for v in (stable, latest) if v}
    if any(pins.values()) or name in ('wizard-ux', 'wizard-agent-service', 'wizard-app-manager'):
        if not sha:
            return 'unknown', 'Merged; can\u2019t tell if it\u2019s released', None
        if stable and pins.get(stable) and contains(token, repo, sha, pins[stable]):
            return 'done', f'In For everyone {stable}', stable   # 0.19.1 (#7): the channel it reached
        if latest and pins.get(latest) and contains(token, repo, sha, pins[latest]):
            return 'testers', f'In For testers {latest}', latest
        return 'waiting', 'Waiting for release', None
    app = store_listing(repo)
    if not app or not app.get('commit'):
        return 'done', 'Done (merged; no Store release to track)', None
    if not sha:
        return 'unknown', 'Merged; can\u2019t tell if it\u2019s released', None
    have = in_build(token, repo, sha, app['commit'])
    if have:
        return 'done', f"Shipped in {app['name']} {app['version']}", f"{app['name']} {app['version']}"
    if have is None:
        return 'unknown', 'Merged; can\u2019t tell if it\u2019s released', None
    return 'app', f"Waiting for {app['name']} release", None


def note_release(iid, wait):
    """0.29: when the Tracker first saw an item's change in For testers / For everyone (DATA_DIR/release_at.json)."""
    slot = {'testers': 'testers', 'done': 'everyone'}.get(wait)
    if not slot:
        return
    with LOCK:
        kept = _read('release_at.json', {})
        kept = kept if isinstance(kept, dict) else {}
        row = kept.get(iid) if isinstance(kept.get(iid), dict) else {}
        if slot in row:
            return
        row[slot] = int(time.time())
        kept[iid] = row
        _write('release_at.json', dict(list(kept.items())[-4000:]))


def release_times(iid, cache=None):
    """{testers_at, everyone_at}: first seen in each release (For everyone falls back to the Shipped-in record)."""
    rel = (cache or {}).get('rel') if cache else None
    shp = (cache or {}).get('shp') if cache else None
    rel = rel if rel is not None else (_read('release_at.json', {}) or {})
    shp = shp if shp is not None else (_read('shipped.json', {}) or {})
    row = rel.get(iid) if isinstance(rel.get(iid), dict) else {}
    kept = shp.get(iid) if isinstance(shp.get(iid), dict) else {}
    return {'testers_at': row.get('testers'), 'everyone_at': row.get('everyone') or kept.get('at')}


def ship_status(token, iid, delivers, open_prs, state, state_reason, labels, base=None):
    """{state shipping|done, wait, text, version}. Open items are always Shipping (approved, not closed yet)."""
    low = {str(l).lower() for l in labels or []}
    if state == 'open' and DEFAULT_LABEL in low:
        return {'state': 'shipping', 'wait': 'default', 'text': 'Waiting for the builder to turn it on for everyone', 'version': None}
    if state == 'closed' and state_reason == 'not_planned':
        return {'state': 'done', 'wait': None, 'text': 'Closed: not planned', 'version': None}
    kept = (_read('shipped.json', {}) or {}).get(iid) if state == 'closed' else None
    if isinstance(kept, dict) and kept.get('text'):
        return {'state': 'done', 'wait': None, 'text': kept_text(kept), 'version': kept.get('version')}
    if not delivers:
        if open_prs:
            return {'state': 'shipping', 'wait': 'waiting', 'text': 'Waiting for the change to merge', 'version': None}
        return {'state': 'done' if state == 'closed' else 'shipping', 'wait': None,
                'text': 'Done' if state == 'closed' else 'Approved', 'version': None}
    key = (iid, json.dumps(delivers, sort_keys=True), state)
    with LOCK:
        hit = _ship.get(key)
    # 0.26.3: stale-while-revalidate. A page load takes any answer under an hour old (the warm-up keeps them fresh);
    # only the background warm-up re-asks once an answer is 3 minutes old.
    if hit and time.time() - hit[0] < (WARM_SECONDS if getattr(_WARMING, 'on', False) else SHIP_STALE_OK):
        return hit[1]
    parts = [ship_one(token, d, base) for d in delivers]
    worst = min(parts, key=lambda p: SHIP_RANK[p[0]])
    note_release(iid, worst[0])
    done = worst[0] == 'done' and state == 'closed'
    if done:   # the newest 'Shipped in' among the parts is when all of it reached everyone
        shipped = [p for p in parts if p[2]]
        text = shipped[-1][1] if shipped else worst[1]
        out = {'state': 'done', 'wait': None, 'text': text, 'version': shipped[-1][2] if shipped else None}
        with LOCK:
            kept = _read('shipped.json', {})
            kept = kept if isinstance(kept, dict) else {}
            kept[iid] = {'text': out['text'], 'version': out['version'], 'at': int(time.time())}
            _write('shipped.json', dict(list(kept.items())[-3000:]))
    else:
        out = {'state': 'shipping', 'wait': worst[0], 'text': worst[1], 'version': worst[2]}
    with LOCK:
        _ship[key] = (time.time(), out)
        if len(_ship) > 1500:
            for k in sorted(_ship, key=lambda k: _ship[k][0])[:300]:
                _ship.pop(k, None)
    return out


def kept_text(kept):
    """A kept 'Shipped in 0.3.134' (a WizardOS release, before 0.19.1) reads 'In For everyone 0.3.134' now."""
    text, v = kept.get('text') or '', kept.get('version') or ''
    return f'In For everyone {v}' if v and text == f'Shipped in {v}' and ' ' not in v else text


def release_of(token, delivers):
    """0.19.1 (wizardos-tracker#7): which release has this item's merged change, from the same lookup as Shipping:
    {text 'Shipped in Messenger 0.2.0-unified.16.34' | 'In For testers 0.3.x' | 'In For everyone 0.3.y' | 'Shipped in
    Mac app 1.2', version}; None when no release has all of it yet (or it ships on merge)."""
    ds = [d for d in delivers or [] if d.get('repo')]
    if not ds:
        return None
    parts = [ship_one(token, d) for d in ds]
    worst = min(parts, key=lambda p: SHIP_RANK[p[0]])
    if worst[2]:
        return {'text': worst[1], 'version': worst[2]}
    if worst[0] == 'done':
        shipped = [p for p in parts if p[2]]
        return {'text': shipped[-1][1], 'version': shipped[-1][2]} if shipped else None
    return None


def _box_facts(raw):
    raw = raw if isinstance(raw, dict) else {}
    rel = raw.get('release') if isinstance(raw.get('release'), str) and VERSION_RE.match(raw.get('release')) else None
    apps = {k: v for k, v in (raw.get('apps') or {}).items() if isinstance(k, str) and APP_ID_RE.match(k)
            and isinstance(v, str) and VERSION_RE.match(v)} if isinstance(raw.get('apps'), dict) else {}
    mac = raw.get('mac') if isinstance(raw.get('mac'), str) and VERSION_RE.match(raw.get('mac')) else None
    return {'release': rel, 'channel': 'latest' if raw.get('channel') == 'latest' else 'stable' if raw.get('channel') == 'stable' else None,
            'apps': dict(list(apps.items())[:40]), 'mac': mac}


def on_box(person, args):
    """POST /api/onbox {box: {release, channel, apps: {id: version}}, items: [{id, delivers: [{repo, number, sha}],
    open_prs}]} -> {items: {id: {state yes|no|unknown, text, fix, parts, apps}}}. `apps` lists app ids whose running
    version the page should send next time (read from /<id>/healthz on the box)."""
    _, acct = account_for(person)
    box = _box_facts(args.get('box'))
    raw = args.get('items') if isinstance(args.get('items'), list) else []
    items = []
    for it in raw[:60]:
        if not isinstance(it, dict) or not isinstance(it.get('id'), str) or len(it['id']) > 220:
            continue
        ds = [{'repo': d['repo'], 'number': d.get('number') if isinstance(d.get('number'), int) else None, 'sha': d['sha'].lower()}
              for d in (it.get('delivers') or [])[:12] if isinstance(d, dict) and isinstance(d.get('repo'), str)
              and REPO_FULL.match(d['repo']) and isinstance(d.get('sha'), str) and (SHA40.match(d['sha'].lower()) or d['sha'] == '')]
        n = it.get('open_prs') if isinstance(it.get('open_prs'), int) and not isinstance(it.get('open_prs'), bool) else 0
        items.append((it['id'], ds, n))
    token, tid = acct['token'], _tid(acct['token'])
    bkey = json.dumps(box, sort_keys=True)
    def one(x):
        iid, ds, n = x
        key = (tid, iid, bkey, json.dumps(ds, sort_keys=True), n)
        with LOCK:
            hit = _verdicts.get(key)
        if hit and time.time() - hit[0] < VERDICT_SECONDS:
            return iid, hit[1]
        v = verdict(token, ds, n, box)
        try:
            v['release'] = release_of(token, ds)
        except Problem:
            v['release'] = None
        with LOCK:
            _verdicts[key] = (time.time(), v)
            if len(_verdicts) > 2000:
                for k in sorted(_verdicts, key=lambda k: _verdicts[k][0])[:500]:
                    _verdicts.pop(k, None)
        return iid, v
    with ThreadPoolExecutor(max_workers=6) as ex:
        out = dict(ex.map(one, items))
    return {'items': out, 'box': box}


def _warm_releases():
    """0.26.2 (Miguel, 11 Nov): keep the release lookups warm, so the Bugs stages (Merged / For testers / For everyone)
    are right the moment the page opens instead of after a 15-second cold lookup."""
    time.sleep(20)
    while True:
        try:
            key = accounts().get('default')
            _WARMING.on = True
            if key:
                for v in ('bugs', 'apps', 'core'):
                    done_list(key, {'view': v})
        except Exception:   # noqa: BLE001 - a warm-up miss only means the next page load does the lookup itself
            pass
        time.sleep(120)


def board_view(person, args):
    """One tab of the board (0.3 shape). 0.17: the same items for everyone; nothing is filtered by who is looking."""
    _, acct = account_for(person)
    tab = args.get('tab') or 'goals'
    if tab not in ('goals', 'test', 'issues'):
        raise Problem(400, 'tab must be goals, test or issues.')
    org = settings()['org']
    repo = _repo(org, args.get('repo'))
    login = (acct.get('login') or '').strip()
    scope = 'repo:' + repo if repo else 'org:' + org
    q = {'goals': f'{scope} is:issue is:open label:goal',
         'test': f'{scope} is:issue is:open label:ready-to-test',   # 0.17: everyone sees the same items
         'issues': f'{scope} is:issue is:open -label:goal'}[tab] + ' sort:updated-desc'
    if args.get('refresh'):
        _forget(acct['token'])
    found = ((graphql(acct['token'], VIEW, {'q': unhide_q(q)}).get('data') or {}).get('search') or {})
    nodes = [n for n in found.get('nodes') or [] if n and n.get('number') and not hidden_repo(_node_repo(n))]
    if tab == 'issues':  # bugs and unlabelled issues; ones waiting for a test live in To test (for everyone)
        def plain_or_bug(n):
            names = {l['name'].lower() for l in (n.get('labels') or {}).get('nodes') or []}
            return names <= {'bug', 'ready-to-test', 'approved', 'accepted'} and 'ready-to-test' not in names
        nodes = [n for n in nodes if plain_or_bug(n)]
    items = [_card(n, tab) for n in nodes]
    t, now = _tid(acct['token']), time.time()
    with LOCK:
        for k in [k for k, v in _tested.items() if now - v > RECENT_SECONDS]:
            _tested.pop(k, None)
        for k in [k for k, v in _made.items() if now - v[0] > RECENT_SECONDS]:
            _made.pop(k, None)
        # 0.17: shared by everyone (same items for every viewer), not per token
        hidden = {i for (k, i) in _tested}
        made = [v for (k, _), v in _made.items() if v[1] == tab]
    if tab == 'test':
        items = [i for i in items if i['id'] not in hidden]
    have = {i['id'] for i in items}
    for _, _, it in made:
        if it['id'] not in have and (not repo or it['repo'].lower() == repo.lower()):
            items.insert(0, it)
    return {'tab': tab, 'org': org, 'repo': repo, 'items': items, 'count': len(items),
            'more': found.get('issueCount', len(items)) > len(nodes) and tab != 'issues', 'login': login}


def _view_nodes(token, q, heavy=False, limit=MAX_ITEMS, query=None):
    """Up to `limit` issues for one view, page by page."""
    nodes, after, total = [], None, 0
    rest = query or PAGES[heavy]
    q = unhide_q(q)
    while len(nodes) < limit:
        data = graphql(token, rest, {'q': q, 'after': after}).get('data') or {}
        found = data.get('search') or {}
        total = found.get('issueCount', total)
        nodes += [n for n in found.get('nodes') or [] if n and n.get('number') and not hidden_repo(_node_repo(n))]
        page = found.get('pageInfo') or {}
        if not page.get('hasNextPage') or not page.get('endCursor'):
            break
        after = page['endCursor']
    return nodes[:limit], total


def _repo_list(arg):
    if not arg:
        return []
    names = [x.strip().lower() for x in str(arg).split(',') if x.strip()]
    return [n.split('/')[-1] for n in names if NAME.match(n.split('/')[-1])]


# 0.18: four tabs. Core features and App features are both label `feature`; which one is derived from the repo
# (policy `core_repos`, defaults CORE_REPOS). 'features' (both) stays for scripts and older views.
VIEWS = ('goals', 'core', 'apps', 'bugs', 'features')
# 0.18.1: an item labelled both `bug` and `feature` (wam#524) is a bug: features exclude `bug`, and Bugs adds a second
# search (VIEW_ALSO) for bug+feature items, because GitHub search can't say '-label:feature OR label:bug'.
VIEW_Q = {'goals': 'label:goal', 'features': 'label:feature -label:goal -label:bug', 'core': 'label:feature -label:goal -label:bug',
          'apps': 'label:feature -label:goal -label:bug', 'bugs': '-label:goal -label:feature'}
VIEW_ALSO = {'bugs': 'label:bug label:feature -label:goal'}
VIEW_KIND = {'goals': 'goal', 'features': 'feature', 'core': 'feature', 'apps': 'feature', 'bugs': 'bug'}
STATS_VIEW = {'goals': 'goals', 'features': 'features', 'core': 'features', 'apps': 'features', 'bugs': 'bugs'}


def _view_search(token, view, q, order='updatedAt', **kw):
    """_view_nodes for a view's query `q` (which contains VIEW_Q[view]), plus the view's VIEW_ALSO search merged in
    (newest updated first, no duplicates)."""
    nodes, total = _view_nodes(token, q, **kw)
    also = VIEW_ALSO.get(view)
    if not also or VIEW_Q[view] not in q:
        return nodes, total
    more, n2 = _view_nodes(token, q.replace(VIEW_Q[view], also), **kw)
    seen = {(n['repository']['nameWithOwner'], n['number']) for n in nodes}
    extra = [n for n in more if (n['repository']['nameWithOwner'], n['number']) not in seen]
    if not extra:
        return nodes, total
    merged = sorted(nodes + extra, key=lambda n: n.get(order) or '', reverse=True)
    return merged[:kw.get('limit', MAX_ITEMS)], total + n2


def _view(args):
    view = args.get('view') or 'goals'
    if view not in VIEWS:
        raise Problem(400, 'view must be goals, core, apps, bugs or features.')
    return view


def view_keeps(view, repo, base=None):
    """Does an item of `repo` belong in `view`? Only core/apps split by repo."""
    if view not in ('core', 'apps'):
        return True
    return is_core(repo, base) == (view == 'core')


GOAL_FIELDS = (' number title url body createdAt updatedAt author { login } assignees(first: 10) { nodes { login } }'
               ' repository { nameWithOwner } labels(first: 20) { nodes { name } }'
               ' comments(last: 50) { totalCount nodes { author { login } body createdAt } }'
               ' timelineItems(itemTypes: [CROSS_REFERENCED_EVENT], last: 100) { nodes { ... on CrossReferencedEvent { source { __typename'
               '  ... on Issue { number title url state stateReason repository { nameWithOwner } labels(first: 15) { nodes { name } } } } } } }')
GOAL_PAGE = 'query($q: String!, $after: String) { ' + _PAGE % (30, GOAL_FIELDS) + ' }'
# 0.28 (Miguel, 11 Nov): 'answer' = Needs an answer: the builder asked a question and paused (label builder:blocked)
OPEN_GROUPS = ('test', 'answer', 'show', 'james', 'airev', 'building', 'queued', 'sorting', 'shipping')
TRIAGE_LABEL = 'triage'   # 0.21: a report waiting for the builder's triage step to move it to the right repo
# 0.19: the builder never picks these up (Miguel, 9 Nov): Oscar builds them directly
BUILDER_SKIPS = ('wizardos-tracker', 'wizardos-builder')
# 0.19.1: the board never shows items of these repos at all (Oscar fixes them directly); filtered server-side, both in
# the GitHub search (so counts match) and on every node returned.
HIDDEN_REPOS = ('humanitylabs-org/wizardos-tracker', 'humanitylabs-org/wizardos-builder')
HIDDEN_Q = ''.join(f' -repo:{r}' for r in HIDDEN_REPOS)


def hidden_repo(repo):
    return str(repo or '').lower() in HIDDEN_REPOS


def _node_repo(n):
    return ((n or {}).get('repository') or {}).get('nameWithOwner') or ''


def unhide_q(q):
    """Add the hidden-repo exclusions to an org-wide search (a single-repo search on a hidden repo finds nothing)."""
    if re.search(r'(?:^|\s)repo:', q) or HIDDEN_Q in q:
        return q   # one repo: the node filter drops a hidden one
    return q + HIDDEN_Q
# 0.19 bug rule (Miguel, 10 Nov): a bug in the filer's check this long with no Doesn't work is closed as fixed (by the
# builder host's sweep, builder-tick); the clock starts no earlier than the rule itself, so older items get 7 days too.
BUG_ACCEPT_SECONDS = 7 * 86400
BUG_RULE_SINCE = '2026-11-10T00:00:00Z'


def waiting_on(card, base=None):
    """0.25 (Miguel, 10 Nov): who an open item waits on right now, so every card and stage answers "whose move is it?".
    -> {'who': [names], 'kind': person | agent | builder | release, 'stuck': bool}."""
    base = base or policy_base()
    g, who_kind = card.get('group'), 'person'
    if card.get('category') == 'bug' and g in ('test', 'airev', 'shipping'):
        return {'who': [], 'kind': 'release', 'stuck': False}   # 0.26: shipped; the check is optional
    if g == 'test':
        opener = card.get('opener') or ''
        on_team = any(t['name'].lower() == opener.lower() for t in base['team'])
        who = [opener] if on_team else ['Anyone on the team']
    elif g == 'james':
        who = [str(n) for n in base.get('core_approvers') or POLICY_DEFAULT['core_approvers']]
    elif g == 'show':
        who = [t['name'] for t in base['team'] if t['name'].lower() in james_people(base)] or ['James']
    elif g == 'airev':
        r = card.get('ai_reviewer') or 'any team agent'
        agents = [a['name'] for tm in base['team'] for a in tm['agents']]
        who, who_kind = ((agents or ['Any team agent']) if r == 'any team agent' else r.split(' or ')), 'agent'
    elif g == 'answer':
        # 0.28: whoever the builder @-mentioned in its question (team names), else the person who filed it
        who = list(card.get('asked') or []) or [card.get('opener') or 'Oscar']
    elif g in ('building', 'queued', 'sorting'):
        if card.get('wait_reason') == 'Oscar builds this one':
            who = ['Oscar']
        else:
            who, who_kind = [base['builder']['name']], 'builder'
    elif g == 'shipping':
        who, who_kind = ['Release'], 'release'
    else:
        who = []
    return {'who': who, 'kind': who_kind, 'stuck': bool(card.get('blocked')) and g != 'answer'}


def waiting_summary(groups):
    """0.25: per stage, how many items wait on whom (most first) and how many are stuck: the bottleneck line."""
    out = {}
    for g, items in groups.items():
        by = {}
        for i in items:
            name = ' or '.join((i.get('waiting_on') or {}).get('who') or [])   # 'James or Miguel' is one entry
            if name:
                by[name] = by.get(name, 0) + 1
        out[g] = {'n': len(items), 'stuck': sum(1 for i in items if (i.get('waiting_on') or {}).get('stuck')),
                  'by': sorted(([k, v] for k, v in by.items()), key=lambda kv: (-kv[1], kv[0].lower()))}
    return out


def queue_reason(repo):
    """Why a queued item waits: the builder takes it next, or Oscar builds it himself (the builder skips the repo)."""
    return 'Oscar builds this one' if (repo or '').split('/')[-1].lower() in BUILDER_SKIPS else 'Next for the builder'


BLOCKED_LABEL = 'builder:blocked'


def item_group(repo, labels, state='open', base=None):
    """0.18: the stage of one bug or feature from its labels (open items; closed ones are Shipping or Done).
    0.28: a queued, building or sorting item labelled builder:blocked is in Needs an answer ('answer')."""
    low = {str(l).lower() for l in labels or []}
    if state == 'closed':
        return 'closed'
    g = _label_group(repo, low, base)
    if g in ('building', 'queued', 'sorting') and any(l.startswith(BLOCKED_LABEL) for l in low):
        return 'answer'
    return g


def _label_group(repo, low, base=None):
    if TRIAGE_LABEL in low:
        return 'sorting'
    if HUMAN_LABEL in low:
        return 'test'
    if AI_LABEL in low:
        return 'airev'
    if SHOW_LABEL in low:
        return 'show'
    if APPROVED_LABEL in low or DEFAULT_LABEL in low:
        return 'shipping'
    if 'building' in low:
        return 'building'
    if category(repo, low, base) == 'core' and JAMES_LABEL not in low:
        return 'james'
    return 'queued'


def linked_items(node, base=None):
    """Bugs and features that mention this goal (the builder tags an item to a goal with a 'Goal: owner/repo#N' line)."""
    kept = _read('shipped.json', {}) or {}
    out, seen = [], set()
    for t in (node.get('timelineItems') or {}).get('nodes') or []:
        x = (t or {}).get('source') or {}
        if x.get('__typename') != 'Issue' or not x.get('url') or x['url'] == node.get('url') or x['url'] in seen:
            continue
        seen.add(x['url'])
        repo = (x.get('repository') or {}).get('nameWithOwner') or ''
        labels = [l['name'] for l in (x.get('labels') or {}).get('nodes') or [] if l]
        cat = category(repo, labels, base)
        if cat == 'goal' or hidden_repo(repo):
            continue
        state = (x.get('state') or '').lower()
        iid = f"{repo}#{x.get('number')}"
        g = item_group(repo, labels, state, base)
        if g == 'closed':
            g = 'shipping' if APPROVED_LABEL in {l.lower() for l in labels} and iid not in kept and (x.get('stateReason') or '').upper() != 'NOT_PLANNED' else 'done'
        out.append({'id': iid, 'repo': repo, 'number': x.get('number'), 'title': x.get('title'), 'url': x['url'],
                    'kind': 'bug' if cat == 'bug' else 'feature', 'category': cat, 'stage': g})
    return out


def goal_card(n, login, base):
    card = _card(n, 'goals')
    assignees = card['assignees']
    author = (n.get('author') or {}).get('login') or ''
    rows = [(((c.get('author') or {}).get('login') or ''), c.get('body') or '', c.get('createdAt')) for c in (n.get('comments') or {}).get('nodes') or []]
    tasks = tasks_of(n.get('body'))
    linked = linked_items(n, base)
    counts = {}
    for it in linked:
        counts[it['stage']] = counts.get(it['stage'], 0) + 1
    can = can_update_goal(login, assignees, base)
    card.update(group='goals', category='goal', opener=person_of(author, base), created_at=n.get('createdAt'),
                labels=[l['name'] for l in (n.get('labels') or {}).get('nodes') or []], tasks=tasks,
                progress={'done': sum(t['done'] for t in tasks), 'total': len(tasks), 'of': 'done-list items'},
                status=latest_status(rows, base), linked=linked, linked_counts=counts,
                can_status=can, status_rule='' if can else goal_updaters_rule(assignees, base),
                can_close=goal_closer(login, base), close_rule=f'Only {goal_closers_text(base)} closes a goal.',
                all_done=bool(tasks) and all(t['done'] for t in tasks), mine=same_person(author, login, base))
    # 0.18.2: the viewer's turn on a goal = they own it (assignee) and it needs a status update (none yet, or old)
    me = person_of(login, base).lower()
    owns = bool(login) and me in {person_of(a, base).lower() for a in assignees or []}
    card['turn'] = owns and (not card['status'] or bool(card['status'].get('stale')))
    return card


def board_split(person, args):
    """0.18 screen. Goals: one card per open goal (status, owners, linked bugs/features by stage). Core features, App
    features, Bugs: open items in stage groups (test = Human review, show = James show-and-tell, james = waiting for
    James, airev, building, queued = Filed / Approved, shipping = approved, not closed yet). Closed items (Shipping until
    released, then Done) come from GET /api/done."""
    _, acct = account_for(person)
    view = _view(args)
    org = settings()['org']
    login = (acct.get('login') or '').strip()
    base = policy_base()
    q = f"org:{org} is:issue is:open {VIEW_Q[view]} sort:updated-desc"
    if args.get('refresh'):
        _forget(acct['token'])
    sview = STATS_VIEW[view]
    common = {'view': view, 'org': org, 'login': login, 'me_person': person_of(login, base), 'names': display_names(base),
              'since': base['since'], 'targets': base['targets'][sview], 'core_repos': base['core_repos'],
              'james': is_james(login, base), 'core_approver': is_core_approver(login, base)}
    if view == 'goals':
        nodes, total = _view_nodes(acct['token'], q, query=GOAL_PAGE, limit=60)
        goals = [goal_card(n, login, base) for n in nodes]
        return {**common, 'groups': {'goals': goals}, 'counts': {'goals': len(goals)}, 'total': total, 'shown': len(goals),
                'more': total > len(nodes), 'repos': [], 'stats': {'pending': True, 'items': []}, 'steps': steps_for('goals', base),
                'test': [], 'doing': goals}
    nodes, total = _view_search(acct['token'], view, q, heavy=False)
    if view in ('core', 'apps'):
        kept = [n for n in nodes if view_keeps(view, n['repository']['nameWithOwner'], base)]
        total = len(kept) if total <= len(nodes) else total
        nodes = kept
    full, _ = _view_search(acct['token'], view, q.replace(' sort:', ' label:ready-to-test,ai-review,show-and-tell,approved sort:'), heavy=True, limit=80)
    if any(BLOCKED_LABEL in {l['name'].lower() for l in (n.get('labels') or {}).get('nodes') or []} for n in nodes):
        # 0.28: Needs an answer cards show the builder's question and whom it asked: they need its comments too
        asks, _ = _view_search(acct['token'], view, q.replace(' sort:', f' label:"{BLOCKED_LABEL}" sort:'), heavy=True, limit=40)
        full = full + asks
    by = {(n['repository']['nameWithOwner'], n['number']): n for n in full}
    nodes = [by.get((n['repository']['nameWithOwner'], n['number']), n) for n in nodes]
    now = time.time()
    with LOCK:
        hidden = {i for (k, i), v in _tested.items() if now - v <= RECENT_SECONDS}
        made = [v for (k, _), v in _made.items() if now - v[0] <= RECENT_SECONDS and v[1] == 'issues']
    groups = {g: [] for g in OPEN_GROUPS}
    james, approver = is_james(login, base), is_core_approver(login, base)
    for n in nodes:
        repo = n['repository']['nameWithOwner']
        names = [l['name'] for l in (n.get('labels') or {}).get('nodes') or []]
        author = (n.get('author') or {}).get('login') or ''
        iid = f"{repo}#{n['number']}"
        group = item_group(repo, names, 'open', base)
        if group == 'test' and iid in hidden:
            continue
        card = _card(n, 'test' if group in ('test', 'airev', 'show', 'shipping') else 'issues')
        cat = category(repo, names, base)
        extra = {}
        if group == 'test':
            ok, rule = human_rule('bug', author, [], login, base)
            extra = {'can_review': ok, 'review_rule': rule, 'turn': ok and same_person(author, login, base)}
        elif group == 'airev':
            extra = {'ai_reviewer': ai_reviewers('bug', author, [], base)[1]}
        elif group == 'james':   # 0.18.2: James or Miguel approve core features; show-and-tell stays James's
            extra = {'can_james': approver, 'james_rule': '' if approver else JAMES_RULE['approve'], 'turn': approver}
        elif group == 'show':
            extra = {'can_james': james, 'james_rule': '' if james else JAMES_RULE['show'], 'turn': james}
        elif group == 'shipping':
            extra = {'delivers_ship': True}
            if cat == 'bug':
                extra['checked'] = checked_of(n, base)
        elif group == 'queued':
            extra = {'wait_reason': queue_reason(repo)}
        elif group == 'sorting':
            extra = {'wait_reason': SORTING_NOTE}
        elif group == 'answer':
            q_ = builder_question([(((c.get('author') or {}).get('login') or ''), c.get('body') or '')
                                   for c in (n.get('comments') or {}).get('nodes') or []], base)
            extra = {'question': q_['question'], 'asked': q_['asked'], 'asked_at': q_['at'],
                     'turn': bool(q_['asked']) and person_of(login, base) in q_['asked']}
        if cat == 'bug' and group in ('airev', 'test'):   # 0.19: bugs are merged (on) before review: already shipping
            extra['shipped'] = True
            extra['can_fail'] = human_rule('bug', author, [], login, base)[0]
        if group == 'test':
            extra['agent_filed'] = filed_by_agent(author, base)
        if cat == 'bug' and group in ('airev', 'test', 'shipping'):
            # 0.26 (Miguel, 11 Nov): a shipped bug waits on nobody. Its stage is where it is in the release cycle; anyone
            # on the team may check it (Works tags it 'Checked by'; Doesn't work sends it back to Building).
            team = on_team(login, base)
            extra.update(shipped=True, can_review=team, can_fail=team, turn=False, checked=checked_of(n, base),
                         review_rule='' if team else 'Only the WizardOS team checks bugs.')
        card.update(group=group, category=cat, kind='bug' if cat == 'bug' else 'feature', mine=same_person(author, login, base),
                    can_close=same_person(author, login, base), opener=person_of(author, base), created_at=n.get('createdAt'),
                    labels=names, blocked=any(str(l).lower().startswith('builder:blocked') for l in names), **extra)
        card['waiting_on'] = waiting_on(card, base)
        groups[group].append(card)
    have = {i['id'] for g in groups.values() for i in g}
    for _, _, it in made:
        if it['id'] not in have and it['repo'].lower().startswith(org.lower() + '/') and view_keeps(view, it['repo'], base):
            cat = category(it['repo'], it.get('labels') or [], base)
            if (view == 'bugs') == (cat == 'bug'):
                own = same_person(it.get('author') or '', login, base)
                g = 'sorting' if TRIAGE_LABEL in {str(x).lower() for x in it.get('labels') or []} else 'james' if cat == 'core' else 'queued'
                ok = approver if g == 'james' else False
                groups[g].insert(0, {**it, 'group': g, 'category': cat, 'mine': own, 'can_close': own, 'can_james': ok, 'turn': ok,
                                     'wait_reason': queue_reason(it['repo']) if g == 'queued' else SORTING_NOTE if g == 'sorting' else None,
                                     'james_rule': '' if ok else JAMES_RULE['approve'], 'opener': person_of(it.get('author') or '', base)})
                groups[g][0]['waiting_on'] = waiting_on(groups[g][0], base)
    stats = view_stats(acct['token'], org, view, cached_only=not args.get('with_stats')) or {'pending': True, 'items': [], 'done': None}
    repos_wanted = _repo_list(args.get('repos'))
    if repos_wanted:
        keep = lambda i: i['repo'].split('/')[-1].lower() in repos_wanted
        groups = {k: [i for i in v if keep(i)] for k, v in groups.items()}
        stats = _filter_stats(stats, keep)
    shown = sum(len(g) for g in groups.values())
    return {**common, 'steps': steps_for(sview, base), 'groups': groups, 'human_reviews': 1,
            'repos': view_repos(acct['token'], org, view, base),
            'waiting': waiting_summary(groups),
            'counts': {k: len(v) for k, v in groups.items()}, 'done': stats.get('done', 0),
            'stats': {**stats, 'summary': None if stats.get('pending') else summarize(stats.get('items', []))},
            'total': total, 'shown': shown, 'more': total > len(nodes),
            # 0.3 shape, kept for older views and scripts
            'test': groups['test'], 'doing': [i for g in ('answer', 'show', 'james', 'airev', 'building', 'queued', 'sorting') for i in groups[g]]}


# ---------- bottleneck stats (label history, cached 5 min per token/org/view) ----------

_stats = {}


def _ts(iso):
    try:
        return datetime.fromisoformat((iso or '').replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def history_of(ev, opened, closed_at, state, now=None):
    """One issue's workflow numbers from its sorted label/close events [(ts, kind, label)], kinds
    LabeledEvent | UnlabeledEvent | ClosedEvent | ReopenedEvent. Seconds; None when a step never happened.
    0.13 line: queued -> building -> ai (label `ai-review`) -> review (label `ready-to-test`, human review) -> done.
    wait = opened -> first `building`; build = first `building` -> first hand-off (`ai-review` or `ready-to-test`);
    ai_rounds / review_rounds = each AI / human review stint that ended (passed on, closed or kicked back);
    total = opened -> closed; tries = times labelled ready-to-test; ai_tries = times labelled ai-review;
    kicks = ready-to-test removed without a close or `approved` within 10 minutes (Doesn't work);
    ai_kicks = ai-review removed without ready-to-test or a close within 10 minutes (AI review fail);
    kick_to_done = first human kick-back -> closed; spent = time in each stage; done = closed time.
    Items from before 0.13 (ready-to-test straight from building) simply skip the AI step."""
    now = now or time.time()
    first_build = next((t for t, k, l in ev if k == 'LabeledEvent' and l == 'building'), None)
    first_ready = next((t for t, k, l in ev if k == 'LabeledEvent' and l in ('ai-review', 'ready-to-test')), None)
    tries = sum(1 for t, k, l in ev if k == 'LabeledEvent' and l == 'ready-to-test')
    ai_tries = sum(1 for t, k, l in ev if k == 'LabeledEvent' and l == 'ai-review')
    kick_times, ai_kick_times = [], []
    for t, k, l in ev:
        if k == 'UnlabeledEvent' and l == 'ready-to-test':
            soon = [x for x in ev if t - 600 <= x[0] <= t + 600]   # 0.18: Works adds approved / show-and-tell first
            if not any(x[1] == 'ClosedEvent' or (x[1] == 'LabeledEvent' and x[2] in ('approved', 'show-and-tell')) for x in soon):
                kick_times.append(t)
        elif k == 'UnlabeledEvent' and l == 'ai-review':
            near = [x for x in ev if t - 600 <= x[0] <= t + 600]
            if not any(x[1] == 'ClosedEvent' or (x[1] == 'LabeledEvent' and x[2] == 'ready-to-test') for x in near):
                ai_kick_times.append(t)
    # Walk the timeline as a small state machine: queued -> building -> ai -> review -> (kick: building) -> done.
    spent = {'queued': 0.0, 'building': 0.0, 'ai': 0.0, 'review': 0.0}
    rounds, ai_rounds = [], []
    stage, since, built, review_from, ai_from = 'queued', opened if opened is not None else (ev[0][0] if ev else now), False, None, None
    kicks_set, ai_kicks_set = set(kick_times), set(ai_kick_times)
    def move(to, t):
        nonlocal stage, since
        if stage in spent and t >= since:
            spent[stage] += t - since
        stage, since = to, t
    for t, k, l in ev:
        if k == 'LabeledEvent' and l == 'building' and stage == 'queued':
            built = True
            move('building', t)
        elif k == 'LabeledEvent' and l == 'ai-review' and stage in ('queued', 'building'):
            built = True
            ai_from = t
            move('ai', t)
        elif k == 'LabeledEvent' and l == 'ready-to-test' and stage in ('queued', 'building', 'ai'):
            built = True
            if stage == 'ai' and ai_from is not None:
                ai_rounds.append(t - ai_from)
            review_from = t
            move('review', t)
        elif k == 'UnlabeledEvent' and l == 'ai-review' and stage == 'ai' and t in ai_kicks_set:
            ai_rounds.append(t - ai_from)
            move('building', t)
        elif k == 'UnlabeledEvent' and l == 'ready-to-test' and stage == 'review' and t in kicks_set:
            rounds.append(t - review_from)
            move('building', t)
        elif k == 'ClosedEvent' and stage != 'done':
            if stage == 'review' and review_from is not None:
                rounds.append(t - review_from)
            if stage == 'ai' and ai_from is not None:
                ai_rounds.append(t - ai_from)
            move('done', t)
        elif k == 'ReopenedEvent' and stage == 'done':
            move('building' if built else 'queued', t)
    if stage != 'done':
        move(stage, now)
    labelled = {}
    for t, k, l in ev:
        if k == 'LabeledEvent':
            labelled[l] = t
        elif k == 'UnlabeledEvent':
            labelled.pop(l, None)
    is_open = (state or '').lower() == 'open'
    ready_since = labelled.get('ready-to-test') if is_open else None
    ai_since = labelled.get('ai-review') if is_open and 'ready-to-test' not in labelled else None
    closed_ts = _ts(closed_at) if closed_at else None
    rnd = lambda x: None if x is None else int(x)
    # current stage by labels (what the board shows), not by the walk
    current = ('done' if not is_open else 'review' if 'ready-to-test' in labelled else 'ai' if 'ai-review' in labelled
               else 'building' if 'building' in labelled else 'queued')
    has = lambda x: x is not None
    # 0.29 (Miguel, 11 Nov): the stats follow the current stages. Waiting for approval (core: opened -> james-approved),
    # Needs an answer (builder:blocked stints) and Show-and-tell (show-and-tell stints), plus when it was handed off.
    approved_at = next((t for t, k, l in ev if k == 'LabeledEvent' and l == 'james-approved'), None)

    def stints(name):
        out, start = [], None
        for t, k, l in ev:
            if k == 'LabeledEvent' and l == name and start is None:
                start = t
            elif start is not None and ((k == 'UnlabeledEvent' and l == name) or k == 'ClosedEvent'):
                out.append(t - start)
                start = None
        return out, (now - start if start is not None and is_open else None)
    answer_rounds, answer_now = stints('builder:blocked')
    show_rounds, show_now = stints('show-and-tell')
    extra = {'approval': rnd(approved_at - opened) if has(approved_at) and has(opened) and approved_at >= opened else None,
             'approved_at': rnd(approved_at), 'handoff_at': rnd(first_ready), 'first_build_at': rnd(first_build),
             'answer_rounds': [int(r) for r in answer_rounds if r >= 0], 'show_rounds': [int(r) for r in show_rounds if r >= 0]}
    spent2 = {'approval': int((approved_at if has(approved_at) else (now if is_open else closed_ts or now)) - opened) if has(opened) else 0,
              'answer': int(sum(answer_rounds) + (answer_now or 0)), 'show': int(sum(show_rounds) + (show_now or 0))}
    return {**extra, 'spent2': spent2, 'wait': rnd(first_build - opened) if has(first_build) and has(opened) else None,
            'build': rnd(first_ready - first_build) if has(first_ready) and has(first_build) and first_ready >= first_build else None,
            'review_rounds': [int(r) for r in rounds if r >= 0], 'ai_rounds': [int(r) for r in ai_rounds if r >= 0],
            'total': rnd(closed_ts - opened) if has(closed_ts) and has(opened) and not is_open else None,
            'tries': tries, 'kicks': len(kick_times), 'ai_tries': ai_tries, 'ai_kicks': len(ai_kick_times),
            'kick_to_done': rnd(closed_ts - kick_times[0]) if kick_times and has(closed_ts) and not is_open else None,
            'waiting_for': rnd(now - ready_since) if ready_since else None,
            'ai_waiting_for': rnd(now - ai_since) if ai_since else None,
            'stage': current, 'spent': {k: int(v) for k, v in spent.items()}, 'staged': built or bool(first_build or first_ready),
            'opened_at': rnd(opened), 'closed_at': rnd(closed_ts) if not is_open else None}


def item_history(node, now=None):
    ev = sorted(((_ts(e.get('createdAt')), e.get('__typename'), ((e.get('label') or {}).get('name') or '').lower())
                 for e in (node.get('timelineItems') or {}).get('nodes') or [] if e and _ts(e.get('createdAt'))), key=lambda x: x[0])
    h = history_of(ev, _ts(node.get('createdAt')), node.get('closedAt'), node.get('state'), now)
    h.pop('spent', None)
    h.pop('spent2', None)
    return {'id': f"{node['repository']['nameWithOwner']}#{node['number']}", 'repo': node['repository']['nameWithOwner'],
            'author': (node.get('author') or {}).get('login') or '', 'state': (node.get('state') or '').lower(), **h}


def spread(xs):
    """Average, median and the typical range p25–p75 (3+ points); `box` (min, p25, median, p75, max) kept for scripts."""
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return {'n': 0, 'avg': None, 'box': None}
    def q(p):
        i = (len(xs) - 1) * p
        lo = int(i)
        hi = min(lo + 1, len(xs) - 1)
        return int(xs[lo] + (xs[hi] - xs[lo]) * (i - lo))
    return {'n': len(xs), 'avg': int(sum(xs) / len(xs)), 'box': [xs[0], q(.25), q(.5), q(.75), xs[-1]] if len(xs) >= 3 else None,
            'median': q(.5), 'p25': q(.25) if len(xs) >= 3 else None, 'p75': q(.75) if len(xs) >= 3 else None}


def line_stats(items):
    """The assembly-line stats (0.8): per bin avg + box, and the return arrow's kick-back numbers.
    rate = share of items that reached Review and were kicked back at least once; per_item = average
    kick-backs among those kicked back."""
    reviewed = [i for i in items if i.get('review_rounds') or i.get('kicks')]  # 0.13.9: decided, not merely waiting
    kicked = [i for i in items if i.get('kicks')]
    to_done = [i['kick_to_done'] for i in kicked if i.get('kick_to_done') is not None]
    ai_seen = [i for i in items if i.get('ai_rounds') or i.get('ai_kicks')]
    ai_kicked = [i for i in items if i.get('ai_kicks')]
    return {'queued': spread([i.get('wait') for i in items]), 'building': spread([i.get('build') for i in items]),
            'ai': spread([r for i in items for r in i.get('ai_rounds') or []]),
            'review': spread([r for i in items for r in i.get('review_rounds') or []]),
            'done': spread([i.get('total') for i in items]),
            # 0.13: two return arrows. `kick` = Human review -> Building (kept as before); `ai_kick` = AI review -> Building.
            'kick': {'reviewed': len(reviewed), 'kicked': len(kicked),
                     'rate': round(len(kicked) / len(reviewed), 3) if reviewed else None,
                     'per_item': round(sum(i['kicks'] for i in kicked) / len(kicked), 2) if kicked else None,
                     'to_done': int(sum(to_done) / len(to_done)) if to_done else None},
            'ai_kick': {'reviewed': len(ai_seen), 'kicked': len(ai_kicked),
                        'rate': round(len(ai_kicked) / len(ai_seen), 3) if ai_seen else None,
                        'per_item': round(sum(i['ai_kicks'] for i in ai_kicked) / len(ai_kicked), 2) if ai_kicked else None}}


def summarize(items):
    """Averages over history records (same maths the view runs after its own filters)."""
    avg = lambda xs: int(sum(xs) / len(xs)) if xs else None
    tried = [i['tries'] for i in items if i['tries']]
    waiting = [i['waiting_for'] for i in items if i['waiting_for'] is not None]
    return {'wait_before_build': avg([i['wait'] for i in items if i['wait'] is not None]),
            'build_time': avg([i['build'] for i in items if i['build'] is not None]),
            'waiting_for_human': avg(waiting), 'waiting_longest': max(waiting) if waiting else None,
            'tries_per_item': round(sum(tried) / len(tried), 2) if tried else None,
            'kicked_back': sum(i['kicks'] for i in items),
            # 0.18.2: items a human said Doesn't work on more than once
            'still_not_working': sum(1 for i in items if (i.get('kicks') or 0) >= 2), 'ai_kicked_back': sum(i.get('ai_kicks') or 0 for i in items), 'done': sum(1 for i in items if i['state'] == 'closed'),
            'items': len(items), 'line': line_stats(items)}


_flows = {}
FLOW_SECONDS = 120
_REST_KIND = {'labeled': 'LabeledEvent', 'unlabeled': 'UnlabeledEvent', 'closed': 'ClosedEvent', 'reopened': 'ReopenedEvent'}


def item_flow(person, args, agent=False):
    """GET /api/flow?repo=&number=: one item's own assembly line from its GitHub timeline (REST timeline API,
    on demand, cached 2 minutes): time spent in Queued, Building and Review, the Done date, kick-backs, stage."""
    _, acct = account_for(person, agent)
    full, number = _target(args or {})
    key = (_tid(acct['token']), full.lower(), number)
    with LOCK:
        hit = _flows.get(key)
    if hit and time.time() - hit[0] < FLOW_SECONDS and not args.get('refresh'):
        return hit[1]
    it = gh(acct['token'], 'GET', f'/repos/{full}/issues/{number}')
    if it.get('pull_request'):
        raise Problem(400, 'That is a pull request, not an issue.')
    rows = []
    for page in range(1, 4):
        got = gh(acct['token'], 'GET', f'/repos/{full}/issues/{number}/timeline?per_page=100&page={page}')
        got = got if isinstance(got, list) else []
        rows += got
        if len(got) < 100:
            break
    ev = sorted(((_ts(e.get('created_at')), _REST_KIND[e.get('event')], ((e.get('label') or {}).get('name') or '').lower())
                 for e in rows if isinstance(e, dict) and e.get('event') in _REST_KIND and _ts(e.get('created_at'))), key=lambda x: x[0])
    h = history_of(ev, _ts(it.get('created_at')), it.get('closed_at'), it.get('state'))
    out = {'id': f'{full}#{number}', 'repo': full, 'number': number, 'state': it.get('state'), **h,
           **release_times(f'{full}#{number}'),
           'still_not_working': (h.get('kicks') or 0) >= 2, 'computed_at': int(time.time())}
    with LOCK:
        _flows[key] = (time.time(), out)
        if len(_flows) > 300:
            for k in sorted(_flows, key=lambda k: _flows[k][0])[:100]:
                _flows.pop(k, None)
    return out


DONE_FIELDS = HEAVY_FIELDS + ' closedAt stateReason closer: timelineItems(itemTypes: [CLOSED_EVENT], last: 1) { nodes { ... on ClosedEvent { actor { login } } } }'
DONE_PAGE = 'query($q: String!, $after: String) { ' + _PAGE % (40, DONE_FIELDS) + ' }'


_done_cache = {}   # 0.26.3: (person, view) -> (time, answer); the warm-up refreshes it, page loads read it


def done_list(person, args):
    """0.26.3: answers from the last lookup (under an hour old) at once; the background warm-up keeps it fresh and
    ?refresh=1 asks again."""
    key = (person_key(person), _view(args))
    hit = _done_cache.get(key)
    if hit and not args.get('refresh') and not getattr(_WARMING, 'on', False) and time.time() - hit[0] < SHIP_STALE_OK:
        return hit[1]
    out = _done_list(person, args)
    _done_cache[key] = (time.time(), out)
    return out


def _done_list(person, args):
    """GET /api/done?view=core|apps|bugs|goals: 0.18 Shipping and Done. Closed items since the reset date (newest first,
    up to 80), each with `ship` {state shipping|done, wait, text, version}, plus `open_ship` for open approved items
    (id -> ship). Release checks run in parallel and are cached (immutable answers for good)."""
    _, acct = account_for(person)
    view = _view(args)
    org = settings()['org']
    if args.get('refresh'):
        _forget(acct['token'])
    base = policy_base()
    nodes, total = _view_search(acct['token'], view, f'org:{org} is:issue is:closed {VIEW_Q[view]} sort:updated-desc', limit=80, query=DONE_PAGE)
    ts = since_ts(base)
    nodes = [n for n in nodes if counts_since('closed', n.get('closedAt'), ts) and view_keeps(view, n['repository']['nameWithOwner'], base)]
    opened = []
    if view != 'goals':
        opened, _ = _view_search(acct['token'], view, f'org:{org} is:issue is:open {VIEW_Q[view]} label:approved,switch-default{',ai-review,ready-to-test' if view == 'bugs' else ''} sort:updated-desc', heavy=True, limit=80)
        opened = [n for n in opened if view_keeps(view, n['repository']['nameWithOwner'], base)]
    token, login = acct['token'], acct.get('login') or ''
    warming = getattr(_WARMING, 'on', False)

    def one(n):
        _WARMING.on = warming   # 0.26.3: the pool's threads re-ask only for the warm-up
        repo = n['repository']['nameWithOwner']
        iid = f"{repo}#{n['number']}"
        labels = [l['name'] for l in (n.get('labels') or {}).get('nodes') or []]
        ds, open_prs = delivered(n)
        st = 'closed' if n.get('closedAt') else 'open'
        if view == 'goals':
            return iid, {'state': 'done', 'wait': None, 'text': 'Closed', 'version': None}
        return iid, ship_status(token, iid, ds, open_prs, st, (n.get('stateReason') or '').lower() or None, labels, base)
    with ThreadPoolExecutor(max_workers=6) as ex:
        ships = dict(ex.map(one, nodes + opened))
    items = []
    for n in nodes:
        card = _card(n, 'goals' if view == 'goals' else 'test')
        author = (n.get('author') or {}).get('login') or ''
        labels = [l['name'] for l in (n.get('labels') or {}).get('nodes') or []]
        cat = category(n['repository']['nameWithOwner'], labels, base)
        sh = ships.get(card['id'])
        card.update(group='done' if sh['state'] == 'done' else 'shipping', category=cat, kind='goal' if cat == 'goal' else 'bug' if cat == 'bug' else 'feature',
                    mine=same_person(author, login, base), opener=person_of(author, base), ship=sh,
                    created_at=n.get('createdAt'), closed_at=n.get('closedAt'), state='closed',
                    state_reason=(n.get('stateReason') or '').lower() or None, labels=labels)
        if cat == 'bug':
            card['checked'] = checked_of(n, base)   # 0.20: Checked by <person> / (no problems in 7 days)
        if cat == 'bug' and card['state_reason'] != 'not_planned':
            card['can_fail'] = card['can_review'] = on_team(login, base)   # 0.26: anyone on the team checks a bug
            card['shipped'] = True
        items.append(card)
    items.sort(key=lambda i: i.get('closed_at') or '', reverse=True)
    repos_wanted = _repo_list(args.get('repos'))
    if repos_wanted:
        items = [i for i in items if i['repo'].split('/')[-1].lower() in repos_wanted]
    open_ship = {f"{n['repository']['nameWithOwner']}#{n['number']}": ships.get(f"{n['repository']['nameWithOwner']}#{n['number']}") for n in opened}
    return {'view': view, 'org': org, 'items': items, 'count': len(items), 'total': total, 'since': base['since'],
            'shipping': [i for i in items if i['group'] == 'shipping'], 'done': [i for i in items if i['group'] == 'done'],
            'open_ship': open_ship}


def _filter_stats(stats, keep):
    if stats.get('pending') or stats.get('error'):
        return stats
    items = [i for i in stats.get('items', []) if keep(i)]
    return {**stats, 'items': items, 'done': sum(1 for i in items if i['state'] == 'closed')}


def stats_view(person, args):
    """GET /api/stats: bottleneck numbers for one view (optionally author= and repos=a,b), 5-minute cache."""
    _, acct = account_for(person)
    view = _view(args)
    stats = view_stats(acct['token'], settings()['org'], view)
    repos_wanted, author = _repo_list(args.get('repos')), (args.get('author') or '').strip().lower()
    if repos_wanted or author:
        stats = _filter_stats(stats, lambda i: (not repos_wanted or i['repo'].split('/')[-1].lower() in repos_wanted)
                              and (not author or i['author'].lower() == author))
    return {'view': view, **stats, 'since': policy_base()['since'], 'summary': summarize(stats.get('items', []))}


def view_stats(token, org, view, cached_only=False):
    key = (_tid(token), org, view)
    with LOCK:
        hit = _stats.get(key)
    if hit and time.time() - hit[0] < STATS_SECONDS:
        return _since_stats(hit[1])
    if cached_only:
        return None
    kind = VIEW_Q[view]
    nodes, total = _view_search(token, view, f'org:{org} is:issue {kind} sort:created-desc', order='createdAt', limit=STATS_MAX, query=STATS_PAGE)
    now = time.time()
    base = policy_base()
    items = [item_history(n, now) for n in nodes if n.get('repository') and view_keeps(view, n['repository']['nameWithOwner'], base)]
    rc = {'rel': _read('release_at.json', {}) or {}, 'shp': _read('shipped.json', {}) or {}}
    for i in items:
        i.update(release_times(i['id'], rc))   # 0.29: Shipping / Merged / For testers / For everyone times
    out = {'items': items, 'done': sum(1 for i in items if i['state'] == 'closed'), 'counted': len(items),
           'total': total, 'computed_at': int(now), 'cache_seconds': STATS_SECONDS}
    with LOCK:
        _stats[key] = (now, out)
    return _since_stats(out)


def _since_stats(out):
    """0.11: drop items closed before the reset date (the cache keeps all, so a changed date applies at once)."""
    iso = policy_base()['since']
    ts = _since(iso)[1]
    items = [i for i in out.get('items', []) if counts_since(i.get('state'), i.get('closed_at'), ts)]
    return {**out, 'items': items, 'done': sum(1 for i in items if i['state'] == 'closed'), 'counted': len(items), 'since': iso}


# ---------- policy (Open -> Build -> Review: who does each step) and the builder switch ----------

# 0.6: the workflow is three hand-off steps, Open -> Build -> Review. The team roster groups GitHub logins by
# person (a person and their own agents count as one person: nobody reviews what they or their agent opened).
POLICY_DEFAULT = {
    'team': [
        {'name': 'Miguel', 'logins': ['mistermizel'], 'agents': [{'login': 'oscarhenrycollins', 'name': 'Oscar'}], 'pronoun': 'he'},
        {'name': 'James', 'logins': ['jdefleur'], 'agents': [{'login': 'defleur-agents', 'name': 'Archimedes'}], 'pronoun': 'he'},
        {'name': 'Jan', 'logins': ['JanMesaric'], 'agents': [], 'pronoun': 'he'},
        {'name': 'Robert', 'logins': ['hacalastudio'], 'agents': [], 'pronoun': 'he'},
    ],
    'builder': {'name': 'WizardOS Builder', 'logins': sorted(BUILDERS)},
    # Who may open and close items in each view: a list of team names, or "team" for everyone on it.
    'open': {'goals': ['James'], 'features': 'team', 'bugs': 'team'},
    # 0.9: target time per step (seconds). Drawn as a tick on each bin's range bar; the median turns
    # green-ish within target, amber over. DATA_DIR/policy.json `targets` overrides (seconds or '1h', '3d', '30m').
    # 0.13: AI review 30m and Human review 1d for both views.
    'targets': {'bugs': {'queued': 3600, 'building': 4 * 3600, 'ai': 1800, 'review': 86400, 'done': 2 * 86400},
                'features': {'queued': 3600, 'building': 4 * 3600, 'ai': 1800, 'review': 86400, 'done': 2 * 86400},
                # 0.10: Goals Queued 1h like Bugs (queue wait is builder pickup, the same for both)
                'goals': {'queued': 3600, 'building': 3 * 86400, 'ai': 1800, 'review': 86400, 'done': 14 * 86400}},
    # 0.13: how many different humans must say Works before Done (1 or 2). DATA_DIR/policy.json `human_reviews`.
    'human_reviews': 1,
    # 0.11: reset date. Done, the Done list and every line stat count only items closed on or after it
    # (open items always count). DATA_DIR/policy.json `since` overrides (ISO date/time, UTC if no zone).
    'since': '2026-11-07T14:45:00Z',
    # 0.18: core repos (everything else is an app). A feature in a core repo waits for James before the builder starts.
    'core_repos': list(CORE_REPOS),
    # 0.18.2 (wizard-os#423): who approves core features before the builder starts (team names). Show-and-tell stays
    # James's (open.goals). DATA_DIR/policy.json `core_approvers` overrides.
    'core_approvers': ['James', 'Miguel'],
}
TARGET_KEYS = {'approval': 'approval', 'james': 'approval', 'answer': 'answer', 'show': 'show', 'shipping': 'shipping',
               'relwait': 'relwait', 'testers': 'testers', 'buildq': 'buildq', 'queued': 'queued', 'open': 'queued', 'building': 'building', 'build': 'building', 'ai': 'ai', 'ai-review': 'ai',
               'ai_review': 'ai', 'review': 'review', 'human': 'review', 'human-review': 'review', 'human_review': 'review',
               'test': 'review', 'done': 'done'}
_UNIT = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 7 * 86400}


def _seconds(v):
    """3600, '1h', '3d', '30m', '2w' -> seconds; anything else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if v > 0 else None
    m = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*', str(v or '').lower())
    return int(float(m.group(1)) * _UNIT[m.group(2)]) or None if m else None


TARGET_MIN, TARGET_MAX = 60, 365 * 86400


def _targets(over):
    out = {v: dict(t) for v, t in POLICY_DEFAULT['targets'].items()}
    if isinstance(over, dict):
        for view, t in over.items():
            if view in out and isinstance(t, dict):
                for k, v in t.items():
                    if k in TARGET_KEYS and _seconds(v) and TARGET_MIN <= _seconds(v) <= TARGET_MAX:
                        out[view][TARGET_KEYS[k]] = _seconds(v)
    return out
STEP_TEXT = {
    'open': {'goals': '{who} opens goals and closes them once every done-list item is ticked. Nobody else adds or closes a goal.',
             'features': 'Anyone on the team, or their own agent, opens a feature to try and closes it once they like it.',
             'bugs': 'Anyone on the team, or their own agent, opens a bug and closes it once it works.'},
    'build': 'The WizardOS Builder writes the code, then labels it ai-review and says what to check.',
    'ai': "The opener's agent (a goal's owner's) checks the build matches the issue (for a goal: that the slice moves its next done-list item forward): pass to Human review, or back to Building.",
    'review': {'bugs': "Whoever filed it tries it and says Works or Still broken.",
               'features': "Whoever filed it tries it (with its switch) and says Works or Needs changes.",
               'goals': "The goal's owners, or {who}, look at the slice: Keep going (the builder takes the next done-list item) or Change direction (with a note), and tick the done-list items that are now done."},
}


def _team_entry(t):
    if not isinstance(t, dict) or not isinstance(t.get('name'), str):
        return None
    logins = [x for x in t.get('logins') or [] if isinstance(x, str) and NAME.match(x)]
    agents = [a for a in t.get('agents') or [] if isinstance(a, dict) and isinstance(a.get('login'), str) and NAME.match(a['login'])]
    pro = t.get('pronoun') if t.get('pronoun') in PRONOUN_POSS else 'they'
    return {'name': t['name'][:40], 'logins': logins, 'agents': [{'login': a['login'], 'name': str(a.get('name') or '')[:40]} for a in agents],
            'pronoun': pro}


def _since(v):
    """'2026-11-07', '2026-11-07T00:00:00Z' -> (iso, unix ts); anything else the default."""
    for raw in (v, POLICY_DEFAULT['since']):
        try:
            d = datetime.fromisoformat(str(raw or '').strip().replace('Z', '+00:00'))
        except ValueError:
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), d.timestamp()
    return POLICY_DEFAULT['since'], 0.0


def since_ts(base=None):
    return _since((base or policy_base())['since'])[1]


def counts_since(state, closed, ts):
    """0.11: an item counts toward Done and line stats when it is open or was closed on/after the reset date.
    `closed` is unix seconds or an ISO string."""
    if (state or '').lower() != 'closed':
        return True
    c = _ts(closed) if isinstance(closed, str) else closed
    return c is not None and c >= ts


def policy_base():
    """Defaults in code; DATA_DIR/policy.json overrides any of team, builder, open, targets, since."""
    over = _policy_file()
    team = [x for x in (_team_entry(t) for t in (over.get('team') if isinstance(over.get('team'), list) else POLICY_DEFAULT['team'])) if x]
    b = over.get('builder') if isinstance(over.get('builder'), dict) else POLICY_DEFAULT['builder']
    builder = {'name': str(b.get('name') or 'WizardOS Builder')[:40], 'logins': sorted({x.lower() for x in b.get('logins') or [] if isinstance(x, str)} | BUILDERS)}
    opn = dict(POLICY_DEFAULT['open'])
    if isinstance(over.get('open'), dict):
        opn.update({k: v for k, v in over['open'].items() if k in VIEWS and (v == 'team' or isinstance(v, list))})
    hr = over.get('human_reviews')
    core = over.get('core_repos')
    core = [str(x).split('/')[-1].lower() for x in core if isinstance(x, str) and NAME.match(str(x).split('/')[-1])] if isinstance(core, list) else list(CORE_REPOS)
    ca = over.get('core_approvers')
    ca = [str(x) for x in ca if isinstance(x, str) and x.strip()] if isinstance(ca, list) and ca else list(POLICY_DEFAULT['core_approvers'])
    return {'team': team, 'builder': builder, 'open': opn, 'targets': _targets(over.get('targets')), 'core_repos': core,
            'core_approvers': ca,
            'since': _since(over.get('since'))[0],
            'human_reviews': hr if isinstance(hr, int) and not isinstance(hr, bool) and hr in (1, 2) else POLICY_DEFAULT['human_reviews']}


def is_core(repo, base=None):
    return (repo or '').split('/')[-1].lower() in set((base or policy_base())['core_repos'])


def category(repo, labels, base=None):
    """0.18: goal | core | app | bug, from labels and the repo (core vs app is never a label)."""
    low = {str(l).lower() for l in labels or []}
    if 'goal' in low:
        return 'goal'
    if 'feature' in low and 'bug' not in low:   # 0.18.1: bug wins when both labels are set (wam#524)
        return 'core' if is_core(repo, base) else 'app'
    return 'bug'


def display_names(base=None):
    """0.10: login -> the name people know (person logins -> person, an agent -> its own name)."""
    out = {}
    for t in (base or policy_base())['team']:
        for x in t['logins']:
            out[x.lower()] = t['name']
        for a in t['agents']:
            out[a['login'].lower()] = a['name'] or t['name']
    for x in (base or policy_base())['builder']['logins']:
        out.setdefault(x.lower(), (base or policy_base())['builder']['name'])
    return out


def _policy_file():
    try:
        over = json.loads((DATA / 'policy.json').read_text())
    except (FileNotFoundError, ValueError, OSError):
        over = {}
    return over if isinstance(over, dict) else {}


def targets_set(person, args, agent=False):
    """POST /api/targets {view, step, seconds}: one step's target for everyone (DATA_DIR/policy.json).
    0.10: any signed-in person may change it (permissions later); agents only with the human's words."""
    view = args.get('view')
    view = STATS_VIEW.get(view, view)
    if view not in ('goals', 'features', 'bugs'):
        raise Problem(400, 'view must be goals, features or bugs.')
    step = TARGET_KEYS.get(str(args.get('step') or '').lower())
    if not step:
        raise Problem(400, 'step must be queued, building, ai, review or done.')
    raw = args.get('seconds', args.get('value'))
    sec = _seconds(raw)
    if not sec or not TARGET_MIN <= sec <= TARGET_MAX:
        raise Problem(400, 'Pick a target between 1 minute and 1 year (seconds, or like 30m, 4h, 3d, 2w).')
    if agent:
        _owner_request(args, agent, 'change step targets')
        who = 'agent'
    else:
        _, acct = account_for(person)
        who = acct.get('login') or person_key(person)
    with LOCK:
        over = _policy_file()
        t = over.get('targets') if isinstance(over.get('targets'), dict) else {}
        v = t.get(view) if isinstance(t.get(view), dict) else {}
        v = {TARGET_KEYS[k]: x for k, x in v.items() if k in TARGET_KEYS}
        v[step] = sec
        t[view] = v
        over['targets'] = t
        meta = over.get('targets_changed') if isinstance(over.get('targets_changed'), dict) else {}
        meta[f'{view}.{step}'] = {'by': who, 'at': int(time.time())}
        over['targets_changed'] = meta
        _write_policy(over)
    base = policy_base()
    return {'view': view, 'step': step, 'seconds': base['targets'][view][step], 'targets': base['targets'][view],
            'all': base['targets'], 'changed_by': who, 'note': 'Shared: the same targets for everyone. Permissions later.'}


def _write_policy(over):
    """DATA_DIR/policy.json, atomically, 0600. Call under LOCK."""
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = DATA / 'policy.json.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(over, f, indent=1)
    tmp.replace(DATA / 'policy.json')


def person_of(login, base=None):
    """The team name a login belongs to (person or their agent); unknown logins are their own person."""
    low = (login or '').lower()
    for t in (base or policy_base())['team']:
        if low in {x.lower() for x in t['logins']} | {a['login'].lower() for a in t['agents']}:
            return t['name']
    return login or ''


def same_person(a, b, base=None):
    return bool(a and b) and person_of(a, base).lower() == person_of(b, base).lower()


def _who(t):
    return {'name': t['name'], 'logins': [{'login': x, 'kind': 'person'} for x in t['logins']]
            + [{'login': a['login'], 'kind': 'agent', 'name': a['name']} for a in t['agents']]}


def steps_for(view, base=None):
    base = base or policy_base()
    team = base['team']
    o = base['open'][view]
    openers = team if o == 'team' else [t for t in team if t['name'].lower() in {str(n).lower() for n in o}]
    names = ' and '.join(t['name'] for t in openers) or 'Nobody'
    g = base['open']['goals']
    goal_names = 'anyone on the team' if g == 'team' else ' or '.join(t['name'] for t in team if t['name'].lower() in {str(n).lower() for n in g}) or 'James'
    return [
        {'key': 'open', 'n': 1, 'name': 'Open', 'group': 'queued', 'count_label': 'Queued',
         'role': 'Anyone on the team' if o == 'team' else names, 'who': [_who(t) for t in openers],
         'about': STEP_TEXT['open'][view].format(who=names), 'next': 'It waits in the queue until the builder picks it up.'},
        {'key': 'build', 'n': 2, 'name': 'Build', 'group': 'building', 'label': 'building', 'count_label': 'Building',
         'role': base['builder']['name'], 'who': [{'name': base['builder']['name'], 'logins': [{'login': x, 'kind': 'agent'} for x in base['builder']['logins']]}],
         'about': STEP_TEXT['build'], 'next': 'AI review (label ai-review).'},
        {'key': 'ai', 'n': 3, 'name': 'AI review', 'group': 'airev', 'label': 'ai-review', 'count_label': 'AI review',
         'role': "The opener's agent" if view != 'goals' else "The goal owner's agent",
         'who': [{'name': t['name'], 'logins': [{'login': a['login'], 'kind': 'agent', 'name': a['name']} for a in t['agents']]} for t in team if t['agents']],
         'except': 'outside the roster, or no agent listed: any team agent',
         'about': STEP_TEXT['ai'], 'next': 'Pass: Human review (label ready-to-test). Fail: back to Building.'},
        {'key': 'review', 'n': 4, 'name': 'Human review', 'group': 'test', 'label': 'ready-to-test', 'count_label': 'Human review',
         'role': 'The filer' if view != 'goals' else f'Goal owner or {goal_names}',
         'who': [_who(t) for t in team] if view != 'goals' else [_who(t) for t in openers],
         'except': '' ,
         'about': STEP_TEXT['review'][view].format(who=goal_names), 'reviews': base['human_reviews'],
         'next': (f"Keep going or Change direction: back to Build for the next slice. {goal_names} closes the goal once every done-list item is ticked." if view == 'goals'
                  else "Works: Shipping until it is in a release for everyone (core features: James's show-and-tell first), then Done. Needs changes: back to Build with the note.")},
    ]


def policy(person=None):
    base = policy_base()
    org = settings()['org']
    views = {v: steps_for(v, base) for v in ('goals', 'features', 'bugs')}
    lines = [f"Goals: opened and closed by {views['goals'][0]['role']}",
             f"Features: opened and closed by {views['features'][0]['role'].lower()}",
             f"Bugs: opened and closed by {views['bugs'][0]['role'].lower()}",
             f"Build: {base['builder']['name']} only · Human review: the filer · Core features: James or Miguel approves first, James after the show-and-tell",
             "Bugs (Miguel, 10 Nov): merged switched on and shipping before review; AI check, then the filer's check after the fact. "
             "Doesn't work reopens it to Building. Done once in a For everyone release and the filer said Works, or 7 days in the filer's check with no Doesn't work."]
    login = None
    try:
        _, acct = account_for(person)
        login = acct.get('login')
    except Problem:
        pass
    return {'version': 2, **base, 'names': display_names(base), 'org': org, 'steps': views, 'me': {'login': login, 'person': person_of(login, base) if login else None},
            # 0.5 shape, kept for scripts
            'goals': {'authors': [x['login'] for w in views['goals'][0]['who'] for x in w['logins']]},
            'builds': {'builders': base['builder']['logins']}, 'lines': lines,
            # 0.18: what the builder must honour
            'builder_rules': {'never_build': ['goal'], 'core_repos': base['core_repos'], 'core_feature_needs_label': JAMES_LABEL,
                              'switch_default_label': DEFAULT_LABEL, 'goal_tag_line': 'Goal: <owner/repo#N> (only on a clear match)',
                              'skip_repos': list(BUILDER_SKIPS),
                              'bug_flow': {'merge': 'switched on, before any review', 'hand_back': AI_LABEL, 'filer_check': HUMAN_LABEL,
                                           'auto_accept_seconds': BUG_ACCEPT_SECONDS, 'auto_accept_since': BUG_RULE_SINCE,
                                           'filer_note': 'Fixed and shipping. Check it when you can'}}, 'editable_file': 'DATA_DIR/policy.json'}


# ---------- 0.13: two-step review (AI review, then Human review; optional second human) ----------

AI_LABEL, HUMAN_LABEL = 'ai-review', 'ready-to-test'
WORKS = re.compile(r"^\s*Tested:\s*works\b", re.I)
RESET = re.compile(r"^\s*(Tested:\s*(?:doesn.t work|needs changes|still broken)|AI review:)", re.I)


def _goal_people(base):
    g = base['open']['goals']
    return {t['name'].lower() for t in base['team']} if g == 'team' else {str(n).lower() for n in g}


def human_rule(kind, opener, assignees, me, base=None):
    """(allowed, rule text) for the human step. Bugs and features: Works / Doesn't work by anyone on the team except
    the opener (a person and their own agent are one person). Goals (0.17): no Works / Doesn't work; the goal's owners
    (assignees) or James say Keep going / Change direction and tick done-list items."""
    base = base or policy_base()
    mine = person_of(me, base).lower()
    if kind == 'goal':
        owners = [person_of(a, base) for a in assignees or []]
        allowed = {o.lower() for o in owners} | _goal_people(base)
        g = base['open']['goals']
        others = [] if g == 'team' else [t['name'] for t in base['team'] if t['name'].lower() in {str(n).lower() for n in g}]
        names = list(dict.fromkeys(owners + others)) or ['James']
        rule = ('Only ' + (' or '.join(names) if len(names) < 3 else ', '.join(names[:-1]) + ' or ' + names[-1]) + ' review this goal.')
        return mine in allowed, rule
    # 0.18 (James, 9 Nov): the FILER tests bugs and features (a person and their own agent are one person). Items filed
    # by someone outside the roster (or the builder) can be tested by anyone on the team.
    if same_person(opener, me, base):
        return True, ''
    who = person_of(opener, base)
    if not any(t['name'].lower() == who.lower() for t in base['team']):
        return True, ''
    # 0.20: plain words. 'James checks this one (he filed it)'; an agent's item names its person ('his agent filed it').
    he = pronoun_of(who, base)
    if filed_by_agent(opener, base):   # 0.19 (wizardos-tracker#31): an agent's items go to its person
        return False, f'{who} checks this one ({PRONOUN_POSS.get(he, "their")} agent filed it)'
    return False, f'{who} checks this one ({he} filed it)'


PRONOUN_POSS = {'he': 'his', 'she': 'her', 'they': 'their'}


def pronoun_of(name, base=None):
    """0.20: the roster's pronoun for a team member (policy team[].pronoun: he, she or they; default they)."""
    for t in (base or policy_base())['team']:
        if t['name'].lower() == (name or '').lower():
            return t.get('pronoun') or 'they'
    return 'they'


def filed_by_agent(login, base=None):
    """Is this login a team member's agent (not the person's own account)?"""
    low = (login or '').lower()
    return any(low == a['login'].lower() for t in (base or policy_base())['team'] for a in t['agents'])


def goal_closer(me, base=None):
    """0.17: only James (policy open.goals) closes or reopens a goal."""
    base = base or policy_base()
    return bool(me) and person_of(me, base).lower() in _goal_people(base)


def goal_closers_text(base=None):
    base = base or policy_base()
    g = base['open']['goals']
    names = [t['name'] for t in base['team'] if g != 'team' and t['name'].lower() in {str(n).lower() for n in g}]
    return ' or '.join(names) or 'James'


def goal_flags(body, assignees, opener, login, base=None):
    """What the signed-in person may do on a goal: steer (owners or James), close (James, once all ticked)."""
    base = base or policy_base()
    tasks = tasks_of(body)
    steer, rule = human_rule('goal', opener, assignees, login, base)
    closer = goal_closer(login, base)
    all_done = bool(tasks) and all(t['done'] for t in tasks)
    return {'tasks': tasks, 'all_done': all_done, 'can_steer': steer, 'steer_rule': rule, 'can_close_goal': closer,
            'close_rule': f'Only {goal_closers_text(base)} closes a goal.'}


def _comment_rows(rows):
    """GraphQL {author{login}, body} or REST {user{login}, body} -> [(login, body)] oldest first."""
    out = []
    for c in rows or []:
        if isinstance(c, dict):
            who = ((c.get('author') or c.get('user') or {}).get('login') or '')
            out.append((who, c.get('body') or ''))
    return out


def review_progress(rows, base=None):
    """Distinct people who said Works in this round of Human review (since the last Doesn't work or AI review
    comment), from the issue's comments. -> {'done', 'need', 'by': [names]}."""
    base = base or policy_base()
    by = []
    for who, body in _comment_rows(rows):
        if RESET.match(body):
            by = []
        elif WORKS.match(body) and who:
            name = person_of(who, base)
            if name.lower() not in {b.lower() for b in by}:
                by.append(name)
    return {'done': len(by), 'need': base['human_reviews'], 'by': by}


def ai_reviewers(kind, opener, assignees, base=None):
    """Whose agent does the AI review: a goal's owners' (else its opener's), a bug's opener's. Returns
    (team names whose agent may do it, display text); names None = any team agent."""
    base = base or policy_base()
    people = [person_of(a, base) for a in assignees or []] if kind == 'goal' else []
    people = people or [person_of(opener, base)]
    with_agents = []
    for name in dict.fromkeys(people):
        t = next((t for t in base['team'] if t['name'].lower() == name.lower()), None)
        if t and t['agents']:
            with_agents.append((t['name'], t['agents'][0]['name'] or t['name']))
    if not with_agents:
        return None, 'any team agent'
    return {n.lower() for n, _ in with_agents}, ' or '.join(a for _, a in with_agents)


def _issue_comments(acct, full, number, count):
    """The last page or two of an issue's comments (REST, oldest first)."""
    if not count:
        return []
    last = max(1, -(-int(count) // 100))
    rows = []
    for page in ([last - 1, last] if last > 1 else [1]):
        got = gh(acct['token'], 'GET', f'/repos/{full}/issues/{number}/comments?per_page=100&page={page}', cache=False)
        rows += got if isinstance(got, list) else []
    return rows


def ai_review(args, passed):
    """MCP ai_review_pass / ai_review_fail (agents only). Needs the issue and a short note. The box's agent must be
    the opener's (or goal owner's) agent per the roster; for openers outside the roster any team agent may."""
    note = _text(args.get('note'), 'note', 3, 4000)
    _, acct = account_for(None, agent=True)
    full, number = _target(args)
    base = policy_base()
    cur = _current(acct, full, number)
    names = [l.get('name', '').lower() for l in cur.get('labels') or []]
    if AI_LABEL not in names:
        raise Problem(409, f'{full}#{number} is not in AI review (label {AI_LABEL}). The builder adds it when it hands over.')
    kind = 'goal' if 'goal' in names else 'bug'
    opener = (cur.get('user') or {}).get('login') or ''
    assignees = [a.get('login') for a in cur.get('assignees') or [] if isinstance(a, dict)]
    allowed, who_text = ai_reviewers(kind, opener, assignees, base)
    box_login = acct.get('login') or ''
    box_person = person_of(box_login, base)
    agent_name = next((t['agents'][0]['name'] for t in base['team'] if t['name'].lower() == box_person.lower() and t['agents']), '') or 'agent'
    if allowed is not None and box_person.lower() not in allowed:
        raise Problem(403, f'AI review of this {kind} is for {who_text}, not this box\'s agent ({agent_name} of {box_person}).', 'not_ai_reviewer')
    head = f"AI review: {'pass' if passed else 'fail'} — {agent_name} (agent of {box_person})"
    gh(acct['token'], 'POST', f'/repos/{full}/issues/{number}/comments', {'body': head + '\n\n' + note})
    # 0.27: a cleanup that removes a dropped feature's switch and code needs no human test either
    finish = passed and ((DEFAULT_LABEL in names and APPROVED_LABEL in names) or 'switch-remove' in names)
    if finish:  # 0.26.4: the human already approved it; the switch is removed, so it closes (Shipping until released)
        _drop_label(acct, full, number, AI_LABEL)
        if DEFAULT_LABEL in names:
            _drop_label(acct, full, number, DEFAULT_LABEL)
        _close(acct, full, number)
    elif passed:  # add Human review first, then drop AI review, so the label timeline never shows a kick-back
        gh(acct['token'], 'POST', f'/repos/{full}/issues/{number}/labels', {'labels': [HUMAN_LABEL]})
        _drop_label(acct, full, number, AI_LABEL)
    else:
        _drop_label(acct, full, number, AI_LABEL)
        try:
            gh(acct['token'], 'POST', f'/repos/{full}/issues/{number}/labels', {'labels': ['building']})
        except Problem:
            pass  # the builder relabels it when it picks it up again
    now = [l.get('name', '').lower() for l in _current(acct, full, number).get('labels') or []]
    if AI_LABEL in now or (passed and not finish and HUMAN_LABEL not in now):
        raise Problem(403, 'The comment is saved, but GitHub did not change the labels (Triage or write access to this repo is needed).')
    with LOCK:
        _flows.pop((_tid(acct['token']), full.lower(), number), None)
        _stats.clear()  # 0.13.9: a review decision shows on the arrows at once
    return {'id': f'{full}#{number}', 'repo': full, 'number': number, 'passed': passed, 'labels': now,
            'stage': ('shipping' if finish else 'review') if passed else 'building', 'saved': True}


def ai_review_human(*_):
    raise Problem(403, "AI review is done by agents, through the Tracker's tools (ai_review_pass / ai_review_fail). "
                       'People review in Human review.', 'agents_only')


def builder_status(seen=False):
    # 0.13.8: the builder host polls GET /api/builder?from=builder before every run and skips the run when Paused.
    if seen:
        with LOCK:
            _write('builder_seen.json', {'at': int(time.time())})
    b = _read('builder.json', {})
    b = b if isinstance(b, dict) else {}
    at = (_read('builder_seen.json', {}) or {}).get('at') or 0
    connected = bool(at) and time.time() - at < 15 * 60
    running = bool(b.get('running', True))
    return {'running': running, 'state': 'Running' if running else 'Paused',
            'updated_at': b.get('updated_at'), 'updated_by': b.get('updated_by'), 'connected': connected,
            'last_seen': at or None,
            'note': 'Paused: the builder finishes its current run, then picks up nothing new.' if not running
            else 'Running: the builder picks up the next item 30 seconds after each run.'}


def builder_set(person, args, agent=False):
    if not isinstance(args.get('running'), bool):
        raise Problem(400, 'running must be true or false.')
    _owner_request(args, agent, 'start or pause the builder')
    if not agent:
        gate(person)
    who = 'agent' if agent else ((accounts()['people'].get(person_key(person)) or {}).get('login') or person_key(person))
    with LOCK:
        _write('builder.json', {'running': args['running'], 'updated_at': int(time.time()), 'updated_by': who})
    return builder_status()


# ---------- 0.7 'Try it' switch per item (mock-up: records the wish per person; nothing runs yet) ----------

TRY_NOTE = 'Mock-up: this will switch the change on in your Wizard once it runs a build with it.'


def _try_key(repo, number):
    if not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo or ''):
        raise Problem(400, 'repo must be owner/name.')
    if not isinstance(number, int) or isinstance(number, bool) or not 0 < number < 10**9:
        raise Problem(400, 'number must be a positive integer.')
    return f'{repo.lower()}#{number}'


def _try_who(person, agent):
    if agent:
        a = accounts()
        return a.get('default') if a.get('default') in a['people'] else next(iter(a['people']), 'owner')
    return person_key(person)


def try_status(person=None, agent=False):
    """GET /api/try: this person's Try it choices, {items: {"owner/repo#12": {on, at}}}."""
    allp = _read('try.json', {})
    allp = allp if isinstance(allp, dict) else {}
    mine = allp.get(_try_who(person, agent)) or {}
    return {'items': {k: v for k, v in mine.items() if isinstance(v, dict) and v.get('on')}, 'connected': False, 'note': TRY_NOTE}


def try_set(person, args, agent=False):
    if not isinstance(args.get('on'), bool):
        raise Problem(400, 'on must be true or false.')
    key = _try_key(args.get('repo'), args.get('number'))
    if agent:
        _owner_request(args, agent, 'switch a change on or off for them')
    else:
        gate(person)
    who = _try_who(person, agent)
    with LOCK:
        allp = _read('try.json', {})
        allp = allp if isinstance(allp, dict) else {}
        mine = allp.get(who) if isinstance(allp.get(who), dict) else {}
        if args['on']:
            mine[key] = {'on': True, 'at': int(time.time())}
        else:
            mine.pop(key, None)
        allp[who] = dict(list(mine.items())[-500:])
        _write('try.json', allp)
    return {**try_status(person, agent), 'key': key, 'on': args['on']}


# ---------- one issue in full, on tap (rendered by GitHub, cleaned here) ----------

class _Clean(HTMLParser):
    """Whitelist HTML cleaner for GitHub's body_html: no scripts, frames, styles, event handlers or
    javascript: links. Images and videos become plain links (the view only loads from itself)."""
    KEEP = {'a', 'p', 'br', 'hr', 'strong', 'b', 'em', 'i', 'del', 's', 'code', 'pre', 'blockquote', 'ul', 'ol', 'li',
            'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'span', 'div', 'sup', 'sub',
            'kbd', 'details', 'summary', 'input', 'dl', 'dt', 'dd', 'tt', 'g-emoji', 'mark', 'ins'}
    DROP = {'script', 'style', 'iframe', 'object', 'embed', 'svg', 'math', 'form', 'noscript', 'template', 'textarea',
            'select', 'button', 'frame', 'frameset', 'head', 'title', 'link', 'meta', 'base', 'audio', 'canvas', 'option'}
    VOID = {'br', 'hr', 'input', 'img', 'source', 'wbr', 'track', 'area', 'col', 'param'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.stack, self.skip, self.links = [], [], 0, 0

    @staticmethod
    def url(v):
        v = (v or '').strip()
        if v.startswith('/') and not v.startswith('//'):
            v = 'https://github.com' + v
        return v if re.match(r'^(https?://|mailto:)', v, re.I) and not any(c in v for c in '\x00\n\r\t') else ''

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.skip:
            if tag in self.DROP and tag not in self.VOID:
                self.skip += 1
            return
        if tag in self.DROP:
            self.skip = 1
            return
        if tag in ('img', 'video'):
            src = self.url(a.get('data-canonical-src') or a.get('src'))
            label = ('Video' if tag == 'video' else 'Image') + (f": {a['alt']}" if a.get('alt') and tag == 'img' and a['alt'] != 'image' else '')
            if src and not self.links:
                self.out.append(f'<a class="bd-media" href="{htmllib.escape(src)}" target="_blank" rel="noopener noreferrer">{htmllib.escape(label)} ↗</a>')
            else:
                self.out.append(htmllib.escape(f'[{label}]'))
            if tag == 'video':
                self.skip = 1  # drop any fallback/source children
            return
        if tag not in self.KEEP:
            return
        name = 'span' if tag == 'g-emoji' else tag
        keep = ''
        if tag == 'a':
            href = self.url(a.get('href'))
            if self.links:
                return  # no nested links
            keep = (f' href="{htmllib.escape(href)}" target="_blank" rel="noopener noreferrer"' if href else '')
            self.links += 1
        elif tag == 'input':
            if (a.get('type') or '').lower() != 'checkbox':
                return
            keep = ' type="checkbox" disabled' + (' checked' if 'checked' in a else '')
        elif tag in ('td', 'th') and (a.get('align') or '') in ('left', 'right', 'center'):
            keep = f' align="{a["align"]}"'
        elif tag == 'ol' and (a.get('start') or '').isdigit():
            keep = f' start="{a["start"]}"'
        self.out.append(f'<{name}{keep}>')
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID and self.stack and self.stack[-1] == tag:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.skip:
            if tag in self.DROP or tag == 'video':
                self.skip -= 1
            return
        if tag not in self.stack:
            return
        while self.stack:
            t = self.stack.pop()
            if t == 'a':
                self.links -= 1
            self.out.append(f"</{'span' if t == 'g-emoji' else t}>")
            if t == tag:
                break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(htmllib.escape(data, quote=False))

    def result(self):
        self.close()
        while self.stack:
            t = self.stack.pop()
            self.out.append(f"</{'span' if t == 'g-emoji' else t}>")
        return ''.join(self.out)


def clean_html(raw):
    c = _Clean()
    c.feed(raw or '')
    return c.result()


HTML_JSON = 'application/vnd.github.html+json'


def issue_detail(person, args):
    """Full issue for the in-app detail: GitHub-rendered body and comments, cleaned. Two REST calls,
    cached 60 s per token like every other read."""
    _, acct = account_for(person)
    full, number = _target(args or {})
    if args.get('refresh'):
        _forget(acct['token'])
    it = gh(acct['token'], 'GET', f'/repos/{full}/issues/{number}', accept=HTML_JSON)
    if it.get('pull_request'):
        raise Problem(400, 'That is a pull request, not an issue.')
    count = it.get('comments') or 0
    rows = gh(acct['token'], 'GET', f'/repos/{full}/issues/{number}/comments?per_page=100', accept=HTML_JSON) if count else []
    who = lambda x: ((x or {}).get('user') or {}).get('login')
    kind = 'goal' if 'goal' in [str(l.get('name', '')).lower() for l in it.get('labels') or [] if isinstance(l, dict)] else 'bug'
    goal = goal_flags(it.get('body') or '', [a.get('login') for a in it.get('assignees') or [] if isinstance(a, dict)],
                      who(it), acct.get('login') or '') if kind == 'goal' else {}
    raw = gh(acct['token'], 'GET', f'/repos/{full}/issues/{number}') if kind == 'goal' else {}
    if goal and isinstance(raw, dict):   # the HTML read has no markdown body; the done-list comes from the raw one
        goal.update({k: v for k, v in goal_flags(raw.get('body') or '', [a.get('login') for a in raw.get('assignees') or [] if isinstance(a, dict)],
                                                 who(it), acct.get('login') or '').items()})
    if kind == 'goal':   # 0.18: status history (comments whose first line is 'Status: green|yellow|red')
        raw_rows = _issue_comments(acct, full, number, count)
        goal['status_history'] = status_of([(((c.get('user') or {}).get('login') or ''), c.get('body') or '', c.get('created_at')) for c in raw_rows if isinstance(c, dict)])
        goal['can_status'] = can_update_goal(acct.get('login') or '', [a.get('login') for a in it.get('assignees') or [] if isinstance(a, dict)])
        goal['status_rule'] = '' if goal['can_status'] else goal_updaters_rule([])
    labels_low = [str(l.get('name', '')).lower() for l in it.get('labels') or [] if isinstance(l, dict)]
    return {'kind': kind, **goal, 'category': category(full, labels_low),
            'can_close': goal['can_close_goal'] if kind == 'goal' else same_person(who(it), acct.get('login') or ''),
            'close_rule': goal['close_rule'] if kind == 'goal' else f'Only {person_of(who(it)) or "whoever opened it"} can close this {kind}.',
            'id': f'{full}#{number}', 'repo': full, 'number': number, 'title': it.get('title'), 'url': it.get('html_url'),
            'state': it.get('state'), 'author': who(it), 'opener': person_of(who(it)), 'created_at': it.get('created_at'), 'updated_at': it.get('updated_at'),
            'labels': [l.get('name') for l in it.get('labels') or [] if isinstance(l, dict)],
            'body_html': clean_html(it.get('body_html')),
            'comments': [{'author': who(c), 'created_at': c.get('created_at'), 'url': c.get('html_url'),
                          'body_html': clean_html(c.get('body_html'))} for c in rows if isinstance(c, dict)],
            'comments_total': count}


def _drop_label(acct, full, number, label):
    with LOCK:
        _stats.clear()  # 0.13.9: any move by the Tracker shows on the arrows at once
    try:
        gh(acct['token'], 'DELETE', f'/repos/{full}/issues/{number}/labels/{label}')
    except Problem as e:
        if e.code != 404:  # already gone is fine
            raise


JAMES_LABEL, SHOW_LABEL, APPROVED_LABEL, DEFAULT_LABEL = 'james-approved', 'show-and-tell', 'approved', 'switch-default'


def _labels(cur):
    return [l.get('name', '').lower() for l in cur.get('labels') or [] if isinstance(l, dict)]


def _add_label(acct, full, number, label):
    gh(acct['token'], 'POST', f'/repos/{full}/issues/{number}/labels', {'labels': [label]})


def _close(acct, full, number, reason='completed'):
    gh(acct['token'], 'PATCH', f'/repos/{full}/issues/{number}', {'state': 'closed', 'state_reason': reason})


WORKS_BY = re.compile(r"^\s*Tested:\s*works\b.*?@([A-Za-z0-9-]+)", re.I | re.S)


def on_team(login, base=None):
    """0.26: is this login a WizardOS team member (or one of their agents)?"""
    who = person_of(login, base).lower()
    return bool(login) and any(t['name'].lower() == who for t in (base or policy_base())['team'])


CHECK_AT = re.compile(r'^[\s>*_`-]*Check(?: it)?\s*:\s*[*_`]*\s*(.+?)\s*$', re.I | re.M)
SHOT_MD = re.compile(r'!?\[([^\]]*)\]\((https://[^)\s]+)\)')
SHOT_LINE = re.compile(r'^[\s>*_`-]*(Before|After)\s*[*_`]*\s*:\s*[*_`]*\s*(?:!?\[[^\]]*\]\()?(https://[^)\s]+)', re.I | re.M)


_HAND_SECTION = re.compile(r'(?im)^[\s>*_#-]*(what changed|what to check|how to check|to check|what is left|left|merged|switch|not checked by me|check)\b[^:\n]*:[*_\s]*')


def _hand_sections(body):
    """'What changed: …' / 'What to check: …' paragraphs of a builder hand-back -> {key: text}."""
    out, marks = {}, list(_HAND_SECTION.finditer(body or ''))
    for n, m in enumerate(marks):
        end = marks[n + 1].start() if n + 1 < len(marks) else len(body)
        out.setdefault(m.group(1).lower(), body[m.end():end].strip())
    # 'For the person who filed this: @x. What to check: …' on one line
    for m in re.finditer(r'(?i)what to check\s*:\s*(.+)', body or ''):
        out.setdefault('what to check', m.group(1).strip())
    return out


def _say(t, limit):
    """Plain words for a person: no markdown, links, code marks, @mentions or 'For the person who filed this'."""
    t = re.sub(r'(?i)for the person (who filed|checking)[^:]*:\s*(@[A-Za-z0-9-]+[,.:]?\s*)?', '', t or '')
    t = re.sub(r'(?i)^\s*(@[A-Za-z0-9-]+[,.:]\s*)+', '', t)
    t = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', t)
    t = re.sub(r'https?://\S+', '', t)
    t = re.sub(r'(?m)^\s*(\d+[.)]|[-*+])\s+', '', t)
    t = re.sub(r'`+|\*{1,3}|(?<![A-Za-z0-9])_{1,2}|_{1,2}(?![A-Za-z0-9])', '', t)
    t = re.sub(r'\s+', ' ', t).strip(' .')
    if len(t) > limit:
        t = t[:limit].rsplit('. ', 1)[0] if '. ' in t[:limit] else t[:limit - 1].rsplit(' ', 1)[0] + '…'
    return t + ('.' if t and t[-1] not in '.…!?' else '')


DEV_CHECK = re.compile(r'(?i)\b(npx|npm (run|test)|vitest|pytest|unittest|node --test|curl |journalctl|systemctl|docker |'
                       r'git (log|show|diff)|checkout\b|tests?/[\w./-]+|\.test\.[jt]s|ls ~|cat ~|run `|gh (api|run))')


def bug_handover(bodies):
    """0.26.1 (Miguel, 11 Nov): the two things a person needs to check a shipped bug, in plain words, from the
    builder's newest hand-back: what changed, and how to check it. {} when there is no hand-back."""
    for body in reversed([b for b in bodies or [] if b]):
        sec = _hand_sections(body)
        changed, how = sec.get('what changed'), sec.get('what to check') or sec.get('how to check') or sec.get('to check')
        if changed or how:
            first = re.split(r'(?<=[.!?])\s+', _say(changed or '', 600))
            how_t = _say(how or '', 520)
            # 0.26.2: behind-the-scenes fixes have nothing a person can see: the builder says 'Visible: no', or its
            # check is a developer's (terminal commands, test files, a code checkout)
            hidden = bool(re.search(r'(?im)^[\s*_-]*visible\s*:\s*no\b', body)) or bool(DEV_CHECK.search(how or ''))
            return {'changed': ' '.join(first[:2])[:320], 'how': how_t, 'dev': hidden}
    return {}


# 0.28 (Miguel, 11 Nov): Needs an answer. These accounts answer the builder as themselves, though the team lists them
# as an agent: Oscar runs Miguel's agent account and is the person a builder question to it reaches.
ANSWER_AS_SELF = {'oscarhenrycollins': 'Oscar'}
MENTION = re.compile(r'(?<![\w/@.-])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))\b')
OPTION = re.compile(r'\s*(\(?[a-zA-Z]\)|[-*+]\s|\d+[.)]\s)')
COMMIT_ID = re.compile(r'\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,64}\b')


def mention_names(body, base=None):
    """The team names a comment @-mentions, in order: a person's login is the person, an agent's login is its owner
    (@defleur-agents is James), except ANSWER_AS_SELF. The builder and logins outside the team are left out."""
    base = base or policy_base()
    bots = {x.lower() for x in base['builder']['logins']} | BUILDERS
    out = []
    for login in MENTION.findall(re.sub(r'```.*?```', ' ', body or '', flags=re.S)):
        low = login.lower()
        if low in bots:
            continue
        name = ANSWER_AS_SELF.get(low) or next((t['name'] for t in base['team'] if low in {x.lower() for x in t['logins']}
                                                 | {a['login'].lower() for a in t['agents']}), None)
        if name and name not in out:
            out.append(name)
    return out


def _question_text(body, base):
    """The builder's question in plain words: @logins become team names, no code blocks, links or commit ids."""
    t = re.sub(r'```.*?```', ' ', body or '', flags=re.S)
    # a run of @logins reads as 'Miguel or Oscar' ('@a @b which…' -> 'Miguel or Oscar: which…')
    run = re.compile(r'@[A-Za-z0-9-]+(?:\s*(?:,|/|\band\b|\bor\b)?\s*@[A-Za-z0-9-]+)*(?P<end>\s*[:,]?)')

    def names(m):
        who = [n for x in re.findall(r'@([A-Za-z0-9-]+)', m.group(0)) for n in mention_names('@' + x, base)]
        start = m.start() == 0 or t[m.start() - 1] == '\n'
        joined = ' or '.join(dict.fromkeys(who))
        word = m.end() < len(t) and t[m.end()].isalpha()
        return joined + (': ' if start and word and joined and not m.group('end').strip() else m.group('end'))
    t = run.sub(names, t)
    t = re.sub(r'`([^`]*)`', lambda m: '' if COMMIT_ID.fullmatch(m.group(1).strip()) or len(m.group(1)) > 60 else m.group(1), t)
    t = COMMIT_ID.sub('', t)
    t = re.sub(r'(?i)\b(sha256|sha|commit|at)\s+(?=[,.;:)]|\s|$)', '', t)
    return t


def _q_clean(s):
    s = _say(s, 400)
    s = re.sub(r'(?i)^(builder\s+)?question\b[^:?]{0,60}:\s*', '', s)
    s = re.sub(r'\(\s*\)|\s+(?=[,.;:?)])', '', s).strip(' :,;')
    return s[:1].upper() + s[1:]


def builder_question(rows, base=None):
    """0.28 (Miguel, 11 Nov): the builder's question on a builder:blocked item, from its newest comment that asks one
    (rows = [(login, body)], oldest first). -> {'question': one or two plain sentences, 'asked': [team names it
    @-mentions], 'at': index of the comment or None}. The sentences that end in '?' (else that contain one); a short
    question gets the sentence before it in the same paragraph. No '?': the paragraph after 'Question'."""
    base = base or policy_base()
    bots = {x.lower() for x in base['builder']['logins']} | BUILDERS
    said = [(k, b) for k, (who, b) in enumerate(rows or []) if (who or '').lower() in bots and b]
    if not said:
        return {'question': '', 'asked': [], 'at': None}
    k, body = next(((k, b) for k, b in reversed(said) if '?' in b or re.search(r'(?i)\bquestion\b', b)), said[-1])
    text = _question_text(body, base)
    paras = [p for p in re.split(r'\n\s*\n', text) if p.strip()]
    found = []
    for want in (lambda x: x.rstrip(' *_)').endswith('?'), lambda x: '?' in x):
        for p in paras:
            lines = [x for x in p.split('\n') if not OPTION.match(x)]   # options the builder lists are not the question
            sents = [x for x in re.split(r'(?<=[.!?])\s+|\n+', '\n'.join(lines)) if x.strip()]
            for n, x in enumerate(sents):
                if want(x) and len(found) < 2:
                    if len(_q_clean(x)) < 60 and n and not found and not want(sents[n - 1]):
                        found.append(sents[n - 1])
                    found.append(x)
        if found:
            break
    if not found:
        m = re.search(r'(?im)^[\s#*_]*question\b[^\n]*$', text)
        if m:
            rest = re.sub(r'(?i)^[\s#*_]*question\b[\s*_:]*', '', m.group(0)).strip()
            after = [p for p in re.split(r'\n\s*\n', text[m.end():]) if p.strip()]
            found = [rest] if len(rest) > 12 else after[:1]
    q = ' '.join(_q_clean(x) for x in found[:2] if _q_clean(x))
    if len(q) > 320:
        q = q[:319].rsplit(' ', 1)[0] + '…'
    return {'question': q, 'asked': mention_names(body, base), 'at': k}

def bug_check_info(bodies):
    """0.26 (Miguel, 11 Nov): what the builder handed over for a bug's optional check, from its comments (newest wins):
    a `Check: <app-id> <where>` line (where to look) and Before / After screenshot links."""
    at, shots = None, {}
    for body in bodies or []:
        body = body or ''
        m = None
        for m in CHECK_AT.finditer(body):
            pass
        if m:
            words = re.sub(r'[`*]+', '', m.group(1)).strip()
            first = words.split(' ', 1)[0].lower()
            rest = words.split(' ', 1)[1] if ' ' in words else ''
            # the page knows which apps are installed: it opens `app` when it is one, else shows the words
            at = {'app': first if APP_ID_RE.match(first) else None, 'where': rest[:200], 'text': words[:240]}
        found = {}
        for k, u in SHOT_LINE.findall(body):
            found[k.lower()] = u
        for alt, u in SHOT_MD.findall(body):
            for k in ('before', 'after'):
                if k in alt.lower() and k not in found:
                    found[k] = u
        if found:
            shots = found
    return at, [{'kind': k, 'url': shots[k]} for k in ('before', 'after') if k in shots]


def checked_of(node, base=None):
    """0.20 (Miguel): a bug's Human check, apart from its release. 'Checked by Miguel': the newest `Tested: works — @login`
    comment (a person's agent counts as the person), else whoever closed it as fixed. Closed by the builder's 7-day sweep
    (the builder account closed it): 'Checked (no problems in 7 days)'. None while the check is pending."""
    base = base or policy_base()
    for c in reversed(((node.get('comments') or {}).get('nodes')) or []):
        m = WORKS_BY.match((c or {}).get('body') or '')
        if m:
            return {'by': person_of(m.group(1), base), 'text': 'Checked by ' + person_of(m.group(1), base), 'at': c.get('createdAt')}
    if not node.get('closedAt') or (node.get('stateReason') or '').lower() == 'not_planned':
        return None
    closer = next((((e or {}).get('actor') or {}).get('login') for e in reversed(((node.get('closer') or {}).get('nodes')) or []) if e), None)
    bots = {x.lower() for x in base['builder']['logins']} | BUILDERS
    if closer and closer.lower() not in bots:
        return {'by': person_of(closer, base), 'text': 'Checked by ' + person_of(closer, base), 'at': node.get('closedAt')}
    swept = any(((c or {}).get('author') or {}).get('login', '').lower() in bots and '7 days' in ((c or {}).get('body') or '')
                for c in ((node.get('comments') or {}).get('nodes')) or [])
    if swept or not closer:
        return {'by': None, 'text': 'Checked (no problems in 7 days)'}
    return {'by': None, 'text': 'Closed on merge (no human check)'}   # older items the builder's merge closed


def tested(person, args, works):
    """Human review, by the filer (0.18). Works: a core feature goes on to James's show-and-tell (label show-and-tell);
    a bug or app feature is approved and closed (label approved): it then shows as Shipping until its merged change is
    in a release that reaches everyone, then Done. Doesn't work: back to Building (a kick-back, counted in stats).
    0.19 bug rule (Miguel, 10 Nov): a bug is merged switched on before any review, so the filer may say Doesn't work at
    any point after the merge: in AI review, in their own check, or after it closed (it reopens to Building)."""
    key, acct = account_for(person)
    full, number = _target(args)
    note = ''
    try:
        shots = [] if works else reporter.screenshots(args)   # 0.24.2: screenshots ride with Doesn't work
    except reporter.ReportError as e:
        raise Problem(400, str(e)) from None
    if not works:   # 0.23.1 (Miguel): Doesn't work always says what's wrong; the note goes into the issue comment
        raw = args.get('note')
        note = raw.strip() if isinstance(raw, str) else ''
        if not note:
            raise Problem(400, 'Say what\u2019s wrong, so the builder knows what to fix.', 'note_required')
        if len(note) > 4000:
            raise Problem(400, 'What went wrong is too long (4000 characters max).')
    login = acct.get('login') or 'someone'
    base = policy_base()
    cur = _current(acct, full, number)
    names = _labels(cur)
    opener = ((cur.get('user') or {}).get('login') or '')
    cat = category(full, names, base)
    closed = (cur.get('state') or 'open') == 'closed'
    late = cat == 'bug' and not works and (closed or AI_LABEL in names) and HUMAN_LABEL not in names
    if cat == 'bug':   # 0.26 (Miguel, 11 Nov): an optional check by anyone on the team, any time after the merge
        if not on_team(login, base):
            raise Problem(403, 'Only the WizardOS team checks bugs.', 'not_team')
        if closed and (cur.get('state_reason') or '').lower() == 'not_planned':
            raise Problem(409, 'This was closed as not planned. File a new bug if it is still broken.')
        late = not works
    elif HUMAN_LABEL not in names and not late:
        if AI_LABEL in names:
            raise Problem(409, 'This is still in AI review: the agent checks it first, then it comes to you.', 'ai_review')
        raise Problem(409, 'This is not waiting for a test any more. Refresh to see the latest.')
    if cat == 'goal':
        raise Problem(409, 'Goals are human-led: they get status updates, not Works / Doesn\u2019t work.', 'goal')
    ok, rule = (True, '') if cat == 'bug' else human_rule('bug', opener, [], login, base)
    if not ok:
        raise Problem(403, rule, 'not_filer')
    if late and closed and (cur.get('state_reason') or '').lower() == 'not_planned':
        raise Problem(409, 'This was closed as not planned. File a new bug if it is still broken.')
    to_default = False
    if works and cat == 'app' and DEFAULT_LABEL not in names:
        # 0.26.4 (Miguel, 11 Nov): an approved app feature ships always on. With a switch it stays open with
        # switch-default; the builder removes the switch, merges, and the AI check closes it (no second human test).
        rows = _issue_comments(acct, full, number, cur.get('comments') or 0)
        sw, how = switch_info([b for w, b in _comment_rows(rows) if w.lower() in BUILDERS], cur.get('body'))
        to_default = bool(sw or how)
    if works:
        body = f'Tested: works \u2014 @{login}'
        if to_default:
            body += '\n\nBuilder: remove the switch (always on for everyone), merge, then hand back for the AI check.'
    else:
        # 0.30 (Robert, 11 Nov call): features need changes (iteration); a shipped bug is still broken
        body = (f"Tested: still broken \u2014 @{login}\n\nWhat still happens: {note}" if cat == 'bug'
                else f"Tested: needs changes \u2014 @{login}\n\nWhat should change: {note}")
    urls, left, why = _attach(acct, shots)
    body += reporter.images_md(urls, len(left))
    gh(acct['token'], 'POST', f'/repos/{full}/issues/{number}/comments', {'body': body})
    shot_warning = None
    if left:
        shot_warning = SHOT_WAIT.format(s='s' if len(left) > 1 else '', why=why or 'GitHub refused')
        try:
            _queue_shots(key, full, number, left, "Screenshots for the Doesn't work note")
        except OSError:
            shot_warning = 'The screenshots could not be attached. Your note was sent.'
    nxt = 'building'
    if works:   # add the next label first, then drop ready-to-test, so the timeline never reads as a kick-back
        nxt = SHOW_LABEL if cat == 'core' else APPROVED_LABEL
        _add_label(acct, full, number, nxt)
        if to_default:
            _add_label(acct, full, number, DEFAULT_LABEL)
        for lab in (HUMAN_LABEL, AI_LABEL) if cat == 'bug' else (HUMAN_LABEL,):
            if lab in names:
                _drop_label(acct, full, number, lab)
        if DEFAULT_LABEL in names:   # the switch is already gone: its approval closes it
            _drop_label(acct, full, number, DEFAULT_LABEL)
        if nxt == APPROVED_LABEL and not closed and not to_default:
            _close(acct, full, number)
    else:
        if closed:   # 0.19: Doesn't work on a shipped bug reopens it to Building
            gh(acct['token'], 'PATCH', f'/repos/{full}/issues/{number}', {'state': 'open'})
            _drop_label(acct, full, number, APPROVED_LABEL)
        for lab in (HUMAN_LABEL, AI_LABEL):
            if lab in names:
                _drop_label(acct, full, number, lab)
        try:
            _add_label(acct, full, number, 'building')
        except Problem:
            pass
    after = _current(acct, full, number)
    now = _labels(after)
    if HUMAN_LABEL in now or (works and nxt not in now) or (late and AI_LABEL in now):
        raise Problem(403, 'Your comment is saved, but GitHub did not change the labels (write access to this repo is needed).')
    with LOCK:
        _tested[(_tid(acct['token']), f'{full}#{number}')] = time.time()
        _ship.clear()
    return {'id': f'{full}#{number}', 'repo': full, 'number': number, 'works': works, 'labels': now, 'saved': True,
            'stage': ('show' if nxt == SHOW_LABEL else 'shipping') if works else 'building', 'state': after.get('state'),
            'switch_default': to_default,
            'reopened': bool(closed and not works), 'reviews': {'done': 1 if works else 0, 'need': 1}, 'final': True,
            'screenshots': urls, 'waiting': len(left), 'warning': shot_warning}


def james_people(base=None):
    """Who approves core features and show-and-tell, and closes goals: policy open.goals (James)."""
    base = base or policy_base()
    return _goal_people(base)


def is_james(login, base=None):
    return bool(login) and person_of(login, base).lower() in james_people(base)


def core_approver_people(base=None):
    """0.18.2: who approves core features before building: policy core_approvers (James or Miguel)."""
    base = base or policy_base()
    return {str(n).lower() for n in base.get('core_approvers') or POLICY_DEFAULT['core_approvers']}


def is_core_approver(login, base=None):
    return bool(login) and person_of(login, base).lower() in core_approver_people(base)


def feedback_pending(person, q=None):
    """0.26.5 GET /api/feedback/pending: the Feedback inbox (Pending approval), read with the person's own token through the
    relay (server-side only; the token never reaches the page)."""
    _, acct = gate(person)
    try:
        return feedback_inbox.pending(acct, refresh=(q or {}).get('refresh') == '1')
    except feedback_inbox.RelayError as e:
        raise Problem(e.code, e.message, e.reason)


def feedback_review(person, args):
    """0.26.5 POST /api/feedback/review {number, action approve|reject, kind?, reason?}: Approve files it in wizard-os (triage + from-feedback;
    0.28.1: `kind` approves it as that kind), Reject declines it with an optional reason the sender sees
    for the builder; Reject closes it as not planned. The relay checks the person is on the team."""
    _, acct = gate(person)
    try:
        out = feedback_inbox.review(acct, args)
    except feedback_inbox.RelayError as e:
        raise Problem(e.code, e.message, e.reason)
    with LOCK:
        _stats.clear()
    _forget(acct['token'])   # the new wizard-os item shows on the next list load
    return out


JAMES_RULE = {'approve': 'Only James or Miguel can approve core features.', 'show': 'Only James approves show-and-tell.'}


def james_decide(person, args):
    """POST /api/james {repo, number, decision approve|decline, note}: a core feature waiting for approval (James or Miguel, 0.18.2). Approve adds
    label james-approved (the builder may start); Decline comments and closes it as not planned."""
    _, acct = account_for(person)
    full, number = _target(args)
    login = acct.get('login') or ''
    base = policy_base()
    decision = args.get('decision')
    if decision not in ('approve', 'decline'):
        raise Problem(400, 'decision must be approve or decline.')
    if not is_core_approver(login, base):
        raise Problem(403, JAMES_RULE['approve'], 'not_james')
    cur = _current(acct, full, number)
    names = _labels(cur)
    if category(full, names, base) != 'core':
        raise Problem(409, 'Only core features wait for approval.', 'not_core')
    if JAMES_LABEL in names or cur.get('state') == 'closed':
        raise Problem(409, 'This is not waiting for approval any more. Refresh to see the latest.')
    note = args.get('note') if isinstance(args.get('note'), str) else ''
    note = note.strip()[:4000]
    body = f"{person_of(login, base) or 'James'}: {'approved' if decision == 'approve' else 'declined'} \u2014 @{login}" + (f'\n\n{note}' if note else '')
    gh(acct['token'], 'POST', f'/repos/{full}/issues/{number}/comments', {'body': body})
    if decision == 'approve':
        _add_label(acct, full, number, JAMES_LABEL)
    else:
        _close(acct, full, number, 'not_planned')
    after = _current(acct, full, number)
    if decision == 'approve' and JAMES_LABEL not in _labels(after):
        raise Problem(403, 'Your comment is saved, but GitHub did not add the label (write access to this repo is needed).')
    with LOCK:
        _stats.clear()
    return {'id': f'{full}#{number}', 'decision': decision, 'labels': _labels(after), 'state': after.get('state'), 'saved': True}


def show_and_tell(person, args):
    """POST /api/show {repo, number}: James approves a core feature after its show-and-tell. Without a switch it is
    closed (approved: Shipping until released). With one it stays open with label switch-default: the builder removes
    the switch / turns it on by default, merges, and closes it."""
    _, acct = account_for(person)
    full, number = _target(args)
    login = acct.get('login') or ''
    base = policy_base()
    if not is_james(login, base):
        raise Problem(403, JAMES_RULE['show'], 'not_james')
    cur = _current(acct, full, number)
    names = _labels(cur)
    if SHOW_LABEL not in names:
        raise Problem(409, 'This is not waiting for show-and-tell. Refresh to see the latest.')
    rows = _issue_comments(acct, full, number, cur.get('comments') or 0)
    said = [b for w, b in _comment_rows(rows) if w.lower() in BUILDERS]
    sw, how = switch_info(said, cur.get('body'))
    switched = bool(sw or how)
    body = f'Show-and-tell: approved \u2014 @{login}' + ('\n\nBuilder: remove the switch (on for everyone by default), merge, then close this issue.' if switched else '')
    gh(acct['token'], 'POST', f'/repos/{full}/issues/{number}/comments', {'body': body})
    _add_label(acct, full, number, APPROVED_LABEL)
    if switched:
        _add_label(acct, full, number, DEFAULT_LABEL)
    _drop_label(acct, full, number, SHOW_LABEL)
    if not switched:
        _close(acct, full, number)
    after = _current(acct, full, number)
    with LOCK:
        _ship.clear()
    return {'id': f'{full}#{number}', 'labels': _labels(after), 'state': after.get('state'), 'switch_default': switched, 'saved': True}


# ---------- 0.27 (Miguel, 11 Nov): Drop this feature (not needed) / Not a bug, won't fix: the '...' menu ----------
DROPPED_LABEL, WONTFIX_LABEL, CLEANUP_LABEL, REMOVE_LABEL = 'dropped', 'wontfix', 'switch-cleanup', 'switch-remove'
# Labels that mean the change is already merged (behind its switch for a feature)
MERGED_LABELS = {AI_LABEL, HUMAN_LABEL, SHOW_LABEL, APPROVED_LABEL, DEFAULT_LABEL}
STAGE_LABELS = ('building', AI_LABEL, HUMAN_LABEL, SHOW_LABEL, DEFAULT_LABEL, 'builder:blocked')


def drop_item(person, args, agent=False):
    """POST /api/drop {repo, number, reason}: anyone on the team drops a feature it doesn't need, or says a bug is not a
    bug / won't fix. A short reason is required and goes into the comment with who said it. The issue closes as not
    planned with label `dropped` (features) or `wontfix` (bugs). A feature whose code is already merged behind its switch
    also gets `switch-cleanup`: the builder's tick files a cleanup issue that removes the switch and its code (through
    the normal AI review). The page turns the switch off on this Wizard before it calls this."""
    _, acct = account_for(person, agent)
    full, number = _target(args)
    reason = _text(args.get('reason'), 'The reason', 3, 300)
    login = acct.get('login') or ''
    base = policy_base()
    if not on_team(login, base):
        raise Problem(403, 'Only the WizardOS team can drop items.', 'not_team')
    cur = _current(acct, full, number)
    names = _labels(cur)
    if (cur.get('state') or 'open') == 'closed':
        raise Problem(409, 'This is already closed. Refresh to see the latest.')
    cat = category(full, names, base)
    if cat == 'goal':
        raise Problem(409, 'Goals are human-led: close a goal from its own card.', 'goal')
    who = person_of(login, base) or login
    if cat == 'bug':
        label, cleanup = WONTFIX_LABEL, False
        body = f"Not a bug / won\u2019t fix \u2014 {who} (@{login})\n\nWhy: {reason}"
        _ensure_label(acct, full, WONTFIX_LABEL, 'ffffff', 'Not a bug, or won\u2019t fix (said in the Tracker)')
    else:
        rows = _issue_comments(acct, full, number, cur.get('comments') or 0)
        sw, how = switch_info([b for w, b in _comment_rows(rows) if w.lower() in BUILDERS], cur.get('body'))
        cleanup = bool((sw or how) and MERGED_LABELS & set(names))
        label = DROPPED_LABEL
        body = f"Dropped (not needed) \u2014 {who} (@{login})\n\nWhy: {reason}"
        if cleanup:
            body += ('\n\nBuilder: its code is merged behind its switch. Open a cleanup PR that removes the switch and '
                     'its code (label switch-cleanup; the tick files the cleanup issue).')
        _ensure_label(acct, full, DROPPED_LABEL, 'cfd3d7', 'Dropped in the Tracker: not needed')
        if cleanup:
            _ensure_label(acct, full, CLEANUP_LABEL, 'c5def5', 'Dropped feature: the builder removes its switch and code')
    gh(acct['token'], 'POST', f'/repos/{full}/issues/{number}/comments', {'body': body})
    _add_label(acct, full, number, label)
    if cleanup:
        _add_label(acct, full, number, CLEANUP_LABEL)
    for lab in STAGE_LABELS:
        if lab in names:
            try:
                _drop_label(acct, full, number, lab)
            except Problem:
                pass
    _close(acct, full, number, 'not_planned')
    after = _current(acct, full, number)
    with LOCK:
        _stats.clear()
        _ship.clear()
    return {'id': f'{full}#{number}', 'repo': full, 'number': number, 'kind': cat, 'label': label, 'cleanup': cleanup,
            'labels': _labels(after), 'state': after.get('state'), 'by': who, 'saved': True}

# ---------- 0.18 goal status: a GitHub comment whose first line is 'Status: green|yellow|red', then the text ----------
STATUS_LINE = re.compile(r'^[ \t>*_]*status[*_]*\s*:\s*[*_]*(green|yellow|red)\b[*_]*[ \t.]*(?:\n|$)', re.I)
STATUS_WORDS = {'green': 'On track', 'yellow': 'At risk', 'red': 'Blocked'}
STATUS_STALE = 7 * 86400


def status_of(rows, base=None):
    """[(login, body, created_at)] oldest first -> status history, newest first: {color, word, text, by, by_person, at}."""
    base = base or policy_base()
    names = display_names(base)
    out = []
    for who, body, at in rows or []:
        m = STATUS_LINE.match(body or '')
        if not m:
            continue
        color = m.group(1).lower()
        text = (body or '')[m.end():].strip()
        out.append({'color': color, 'word': STATUS_WORDS[color], 'text': _plain(text, 600), 'by': who,
                    'by_name': names.get((who or '').lower(), who), 'by_person': person_of(who, base), 'at': at})
    out.reverse()
    return out


def latest_status(rows, base=None, now=None):
    h = status_of(rows, base)
    if not h:
        return None
    s = dict(h[0])
    t = _ts(s.get('at')) if isinstance(s.get('at'), str) else None
    s['stale'] = bool(t) and (now or time.time()) - t > STATUS_STALE
    return s


def goal_updaters_rule(assignees, base=None):
    base = base or policy_base()
    return 'Only the goal\u2019s owners or James update this.'


def can_update_goal(login, assignees, base=None):
    base = base or policy_base()
    me = person_of(login, base).lower()
    return bool(login) and (me in {person_of(a, base).lower() for a in assignees or []} or me in james_people(base))


def goal_status_set(person, args, agent=False):
    """POST /api/goal/status {repo, number, color green|yellow|red, text}; MCP goal_status_set. The goal's owners or
    James (an agent posts for its own person, who must be one of them)."""
    _, acct = account_for(person, agent)
    full, number = _target(args)
    login = acct.get('login') or ''
    base = policy_base()
    color = str(args.get('color') or '').strip().lower()
    if color not in STATUS_WORDS:
        raise Problem(400, 'color must be green, yellow or red.')
    text = _text(args.get('text'), 'Status text', 3, 600)
    cur = _current(acct, full, number)
    if 'goal' not in _labels(cur):
        raise Problem(409, 'Only goals have a status.', 'not_goal')
    assignees = [a.get('login') for a in cur.get('assignees') or [] if isinstance(a, dict)]
    if not can_update_goal(login, assignees, base):
        raise Problem(403, goal_updaters_rule(assignees, base), 'not_goal_owner')
    gh(acct['token'], 'POST', f'/repos/{full}/issues/{number}/comments', {'body': f'Status: {color}\n\n{text}'})
    _forget(acct['token'])
    return {'id': f'{full}#{number}', 'saved': True, 'status': {'color': color, 'word': STATUS_WORDS[color], 'text': text,
            'by': login, 'by_person': person_of(login, base), 'at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'stale': False}}


def goal_status_get(person, args, agent=False):
    """GET /api/goal/status?repo=&number=; MCP goal_status_get: the latest status and the whole history (newest first)."""
    _, acct = account_for(person, agent)
    full, number = _target(args)
    cur = _current(acct, full, number)
    if 'goal' not in _labels(cur):
        raise Problem(409, 'Only goals have a status.', 'not_goal')
    rows = []
    count = cur.get('comments') or 0
    for page in range(1, max(1, -(-int(count) // 100)) + 1) if count else []:
        got = gh(acct['token'], 'GET', f'/repos/{full}/issues/{number}/comments?per_page=100&page={page}', cache=False)
        rows += got if isinstance(got, list) else []
    hist = status_of([(((c.get('user') or {}).get('login') or ''), c.get('body') or '', c.get('created_at')) for c in rows if isinstance(c, dict)])
    latest = latest_status([(((c.get('user') or {}).get('login') or ''), c.get('body') or '', c.get('created_at')) for c in rows if isinstance(c, dict)])
    assignees = [a.get('login') for a in cur.get('assignees') or [] if isinstance(a, dict)]
    return {'id': f'{full}#{number}', 'title': cur.get('title'), 'owners': [person_of(a) for a in assignees], 'latest': latest, 'history': hist}


def new_item(person, args):
    _, acct = account_for(person)
    kind = args.get('type') or 'issue'
    if kind not in ('goal', 'issue'):
        raise Problem(400, 'type must be goal or issue.')
    if kind == 'goal' and (acct.get('login') or '').lower() not in GOAL_AUTHORS:
        raise Problem(403, 'Only goal authors can add Goals. Add it as an Issue instead.')
    full = _repo(settings()['org'], args.get('repo') or DEFAULT_REPO)
    title = _text(args.get('title'), 'Title', 3, 200)
    body = args.get('body') or ''
    if not isinstance(body, str) or len(body) > 8000:
        raise Problem(400, 'The description is too long (8000 characters max).')
    made = _create(acct, full, title, body.strip(), 'goal' if kind == 'goal' else 'bug')
    owner, name = full.split('/')
    node = ((graphql(acct['token'], VIEW_ONE, {'o': owner, 'r': name, 'n': made['number']}, cache=False).get('data') or {}).get('repository') or {}).get('issue')
    tab = 'goals' if kind == 'goal' else 'issues'
    card = _card(node, tab) if node else {'id': made['id'], 'repo': full, 'number': made['number'], 'title': made['title'], 'url': made['url'],
                                         'author': made.get('author'), 'kind': 'goal' if kind == 'goal' else 'bug'}
    if made.get('labelled'):
        with LOCK:
            if not hidden_repo(card.get('repo')):   # 0.19.1: never shown, even right after filing
                _made[(_tid(acct['token']), card['id'])] = (time.time(), tab, card)
    return {'tab': tab, 'item': card, 'warning': made.get('warning')}


# ---------- 0.20 Report or request (WizardOS Reporter: logic in app/report.py) ----------

def report_next(kind, where, org, base=None):
    """What happens next, in the owner's words, for the 'where it landed' line."""
    base = base or policy_base()
    repo = where['repo']
    if kind == 'feature' and where['area'] == 'core':
        names = [str(n) for n in base.get('core_approvers') or []] or ['James', 'Miguel']
        return 'waits for ' + (' or '.join(names) if len(names) < 3 else ', '.join(names[:-1]) + ' or ' + names[-1]) + ' to approve'
    if repo.split('/')[0].lower() != (org or '').lower():
        return 'its maintainer picks it up'
    if repo.split('/')[-1].lower() in BUILDER_SKIPS:
        return 'Oscar builds this one'
    return 'the builder starts soon'


def report_options(person):
    _, acct = account_for(person)
    return {**reporter.options(), 'core_repos': policy_base()['core_repos'],
            'screenshots': bool(acct.get('token') and _media_ready(acct['token']))}


SORTING_NOTE = 'Sorting… the builder files it in the right place in a minute or two'
_labels_made = set()   # (repo, label) known to exist


def _ensure_label(acct, full, name, color, desc):
    """Create a label in a repo if it is missing (a 422 'already exists' is fine). Never fails the caller."""
    if (full.lower(), name) in _labels_made:
        return
    try:
        gh(acct['token'], 'POST', f'/repos/{full}/labels', {'name': name, 'color': color, 'description': desc})
    except Problem:
        pass
    _labels_made.add((full.lower(), name))


def _upload_shot(acct, raw, ext):
    """0.21: put the screenshot in the team's media repo (MEDIA_REPO) as the filer -> a URL the issue shows.
    Needs the GitHub App's Contents read and write permission on the org installation (0.24.2)."""
    import base64
    import secrets
    path = time.strftime('reports/%Y/%m/', time.gmtime()) + secrets.token_hex(8) + '.' + ext
    out = gh(acct['token'], 'PUT', f'/repos/{MEDIA_REPO}/contents/{path}',
             {'message': 'Report or request screenshot', 'content': base64.b64encode(raw).decode()})
    return ((out.get('content') or {}).get('html_url') or f'https://github.com/{MEDIA_REPO}/blob/main/{path}') + '?raw=true'


SHOTS_BODY = reporter.SHOT_COUNT * reporter.SHOT_MAX * 4 // 3 + 1024 * 1024   # every screenshot, base64, plus text
SHOT_WAIT = ('The screenshot{s} could not upload yet ({why}). Your text was sent; the Tracker adds the '
             'screenshot{s} to it by itself as soon as GitHub allows it.')
PENDING_DIR = 'media-pending'      # 0.24.2: screenshots kept until they upload (never lost with the text)
PENDING_DAYS = 30


def _attach(acct, shots):
    """Upload each screenshot -> (urls, left, why). After one refusal the rest wait too (same token, same answer)."""
    urls, left, why = [], [], None
    for raw, ext in shots:
        if left:
            left.append((raw, ext))
            continue
        try:
            urls.append(_upload_shot(acct, raw, ext))
        except Problem as e:
            why = e.message.rstrip('.')
            left.append((raw, ext))
    return urls, left, why


def _queue_shots(key, full, number, shots, what):
    """Keep screenshots that could not upload (0600 files) so _retry_pending adds them to the issue later."""
    import secrets
    d = _private() / PENDING_DIR / (time.strftime('%Y%m%d%H%M%S', time.gmtime()) + '-' + secrets.token_hex(4))
    d.mkdir(parents=True, mode=0o700)
    names = []
    for n, (raw, ext) in enumerate(shots, 1):
        name = f'{n}.{ext}'
        fd = os.open(d / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(raw)
        names.append(name)
    (d / 'meta.json').write_text(json.dumps({'person': key, 'repo': full, 'number': number, 'files': names,
                                             'what': what, 'at': time.time()}))


def _retry_pending():
    """Upload waiting screenshots with their filer's account and add them to the issue in one comment.
    Follows a moved issue (the builder transfers triage reports). Gives up after PENDING_DAYS."""
    import shutil
    root = PRIVATE / PENDING_DIR
    if not root.is_dir():
        return 0
    done = 0
    people = accounts()['people']
    for d in sorted(root.iterdir()):
        try:
            meta = json.loads((d / 'meta.json').read_text())
        except (OSError, ValueError):
            shutil.rmtree(d, ignore_errors=True)
            continue
        if time.time() - meta.get('at', 0) > PENDING_DAYS * 86400:
            shutil.rmtree(d, ignore_errors=True)
            continue
        acct = people.get(meta.get('person'))
        if not acct or not acct.get('token'):
            continue
        shots = [((d / n).read_bytes(), n.rsplit('.', 1)[-1]) for n in meta.get('files') or [] if (d / n).is_file()]
        urls, left, _ = _attach(acct, shots)
        if left:   # still refused: try again next round (anything uploaded is re-uploaded then; harmless)
            continue
        try:
            cur = gh(acct['token'], 'GET', f"/repos/{meta['repo']}/issues/{meta['number']}", cache=False)
            full = '/'.join((cur.get('repository_url') or '').split('/')[-2:]) or meta['repo']
            gh(acct['token'], 'POST', f"/repos/{full}/issues/{cur.get('number') or meta['number']}/comments",
               {'body': (meta.get('what') or 'Screenshots') + ' (uploaded after the text):' + reporter.images_md(urls)})
        except Problem:
            continue
        shutil.rmtree(d, ignore_errors=True)
        done += 1
    return done


def report_file(person, args, agent=False):
    """0.21: file one two-step report in <org>/wizard-os with labels triage + bug/feature (version, device and an
    optional screenshot filled in). The builder's triage step moves it to the right repo within a tick or two."""
    key, acct = account_for(person, agent)
    org = settings()['org']
    try:
        kind, text = reporter.compose(args)
        shots = reporter.screenshots(args)
    except reporter.ReportError as e:
        raise Problem(400, str(e)) from None
    where = reporter.triage_where(org)
    warning = None
    urls, left, why = _attach(acct, shots)
    if left:   # 0.24.2: the text is filed anyway; the screenshots wait on this box and are added later
        warning = SHOT_WAIT.format(s='s' if len(left) > 1 else '', why=why or 'GitHub refused')
    _ensure_label(acct, where['repo'], reporter.TRIAGE_LABEL, 'fbca04', 'Report or request: the builder moves it to the right repo')
    labels = [reporter.LABELS[kind], reporter.TRIAGE_LABEL]
    made = _create(acct, where['repo'], reporter.title_of(text),
                   reporter.triage_body(kind, text, args, shot_urls=urls, waiting=len(left)), labels)
    url = urls[0] if urls else None
    if left:
        try:
            _queue_shots(key, where['repo'], made['number'], left, 'Screenshots')
        except OSError:
            warning = 'The screenshots could not be attached. Your text was sent.'
    card = {'id': made['id'], 'repo': where['repo'], 'number': made['number'], 'title': made['title'], 'url': made['url'],
            'author': made.get('author'), 'kind': kind, 'labels': made.get('labels') or labels}
    if made.get('labelled'):
        try:
            owner, name = where['repo'].split('/')
            node = ((graphql(acct['token'], VIEW_ONE, {'o': owner, 'r': name, 'n': made['number']}, cache=False).get('data') or {}).get('repository') or {}).get('issue')
            if node:
                card = {**_card(node, 'issues'), 'labels': [l['name'] for l in (node.get('labels') or {}).get('nodes') or []]}
        except Problem:
            pass
        with LOCK:
            _made[(_tid(acct['token']), card['id'])] = (time.time(), 'issues', card)
    view = 'bugs' if kind == 'bug' else 'core'
    return {'item': card, 'url': made['url'], 'repo': where['repo'], 'kind': kind, 'area': 'triage', 'view': view,
            'landed': reporter.SENT, 'sent': reporter.SENT, 'screenshot': url, 'screenshots': urls, 'waiting': len(left),
            'warning': ' '.join(x for x in (warning, made.get('warning')) if x) or None}


# ---------- MCP ----------

FILTERS = {'repo': {'type': 'string', 'description': 'Repo name in the org, or owner/name.'},
           'type': {'type': 'string', 'enum': ['all', 'goal', 'bug']},
           'state': {'type': 'string', 'enum': ['open', 'closed']},
           'org': {'type': 'string', 'description': 'GitHub org; defaults to the app setting.'}}
ISSUE = {'issue': {'type': 'string', 'description': 'owner/repo#123 or the issue URL.'},
         'repo': {'type': 'string'}, 'number': {'type': 'integer'}}
ASKED = {'owner_request': {'type': 'string', 'description': "Required: the human's own words asking for this action."}}
TOOLS = [
    {'name': 'board_list', 'description': 'List Goals and Bugs on the board (GitHub issues labelled goal or bug in the org). Goals include done-list progress and open linked PR counts.', 'inputSchema': {'type': 'object', 'properties': FILTERS}},
    {'name': 'board_get', 'description': 'Read one Goal or Bug: body, done-list, linked PRs and recent comments.', 'inputSchema': {'type': 'object', 'properties': ISSUE}},
    {'name': 'board_report_bug', 'description': 'Report a small, specific bug (label bug). Anyone on the team or their agent may report one; it is ready to work as soon as it is filed.', 'inputSchema': {'type': 'object', 'properties': {'repo': {'type': 'string'}, 'title': {'type': 'string', 'minLength': 3, 'maxLength': 200}, 'body': {'type': 'string', 'description': 'What happens, what should happen, how to see it.'}}, 'required': ['repo', 'title']}},
    {'name': 'board_report_feature', 'description': 'Add a feature or improvement to try (label feature). Anyone on the team may add one. The builder builds it behind an off-by-default switch; the team tries it with Try it in the Tracker and keeps or drops it.', 'inputSchema': {'type': 'object', 'properties': {'repo': {'type': 'string'}, 'title': {'type': 'string', 'minLength': 3, 'maxLength': 200}, 'body': {'type': 'string', 'description': 'What it should do, where, and why it is better.'}}, 'required': ['repo', 'title']}},
    {'name': 'report_or_request', 'description': "File what an owner said is broken or wants, like the 'Report or request' form: kind bug (something's broken) or feature (something new), text = the owner's words (bug: what happened, what should happen; feature: what and why; name the app if it is about one). It lands in wizard-os with label triage; the builder moves it to the right repo within a few minutes and comments where.", 'inputSchema': {'type': 'object', 'properties': {'kind': {'type': 'string', 'enum': ['bug', 'feature']}, 'about': {'type': 'string'}, 'app_name': {'type': 'string'}, 'text': {'type': 'string', 'minLength': 8, 'maxLength': 8000}, 'wizard_version': {'type': 'string'}, 'device': {'type': 'string'}}, 'required': ['kind', 'text']}},
    {'name': 'board_comment', 'description': 'Add a comment to a Goal or Bug.', 'inputSchema': {'type': 'object', 'properties': {**ISSUE, 'body': {'type': 'string', 'minLength': 1}}, 'required': ['body']}},
    {'name': 'board_add_goal', 'description': 'HUMANS OPEN GOALS. Use only when a human explicitly asked you to add this goal for them; pass their words as owner_request.', 'inputSchema': {'type': 'object', 'properties': {'repo': {'type': 'string'}, 'title': {'type': 'string'}, 'done': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Done-list lines (1-20).'}, 'body': {'type': 'string'}, **ASKED}, 'required': ['repo', 'title', 'done', 'owner_request']}},
    {'name': 'board_close', 'description': 'Close a Bug when a human asked you to (pass their words). Goals are refused: only humans close goals, in the app.', 'inputSchema': {'type': 'object', 'properties': {**ISSUE, 'reason': {'type': 'string', 'enum': ['completed', 'not_planned']}, **ASKED}, 'required': ['owner_request']}},
    {'name': 'builder_status', 'description': 'Is the WizardOS Builder switch Running or Paused? (Mock-up: not wired to the builder host yet.)', 'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'builder_set', 'description': 'Set the WizardOS Builder switch (running true = Running, false = Paused) only when a human asked; pass their words as owner_request.', 'inputSchema': {'type': 'object', 'properties': {'running': {'type': 'boolean'}, **ASKED}, 'required': ['running', 'owner_request']}},
    {'name': 'try_status', 'description': "Which items the signed-in person switched 'Try it' on for (mock-up: records the wish, nothing runs yet).", 'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'try_set', 'description': "Switch 'Try it' on or off for one item (repo owner/name + number) only when a human asked; pass their words as owner_request. Mock-up.", 'inputSchema': {'type': 'object', 'properties': {'repo': {'type': 'string'}, 'number': {'type': 'integer'}, 'on': {'type': 'boolean'}, **ASKED}, 'required': ['repo', 'number', 'on', 'owner_request']}},
    {'name': 'board_flow', 'description': "One item's own assembly line from its GitHub label timeline: seconds spent in Queued, Building and Review, closed time, kick-backs (ready-to-test removed without a close), current stage.", 'inputSchema': {'type': 'object', 'properties': ISSUE}},
    {'name': 'targets_set', 'description': "Change one step's target time for everyone (view goals|bugs, step queued|building|ai|review|done (ai = AI review, review = Human review), seconds or '4h'/'3d') only when a human asked; pass their words as owner_request. Current targets: GET /api/policy.", 'inputSchema': {'type': 'object', 'properties': {'view': {'type': 'string', 'enum': ['goals', 'features', 'bugs']}, 'step': {'type': 'string', 'enum': ['queued', 'building', 'ai', 'review', 'done']}, 'seconds': {'description': 'Seconds (60 to 31536000) or a string like 30m, 4h, 3d, 2w.', 'type': ['integer', 'string']}, **ASKED}, 'required': ['view', 'step', 'seconds', 'owner_request']}},
    {'name': 'ai_review_pass', 'description': "AI REVIEW (agents only). The builder hands work over with label ai-review. If you are the opener's agent (a goal's owner's agent; for openers outside the roster any team agent), check the build matches the issue's scope (for a goal: that this slice moves its next done-list item forward), then pass it on to Human review (ai-review -> ready-to-test) with a short note saying what you checked and what a human should try (write the steps after 'Human test:'). Bugs and features then get Works / Doesn't work from whoever filed them; core features then go to James's show-and-tell. Goals are human-led and never built.", 'inputSchema': {'type': 'object', 'properties': {**ISSUE, 'note': {'type': 'string', 'minLength': 3, 'maxLength': 4000, 'description': 'What you checked, and what the human reviewer should try.'}}, 'required': ['note']}},
    {'name': 'ai_review_fail', 'description': "AI REVIEW (agents only). The build does not match the issue's scope (for a goal: the slice does not move a done-list item forward): send it back to Building (removes ai-review, adds building) with a short note saying what is missing or wrong; the builder picks it up again. Counted on the AI review -> Building return arrow.", 'inputSchema': {'type': 'object', 'properties': {**ISSUE, 'note': {'type': 'string', 'minLength': 3, 'maxLength': 4000, 'description': 'What is missing or wrong, for the builder.'}}, 'required': ['note']}},
    {'name': 'goal_status_set', 'description': "Post a goal's status for its owner (the goal's owners or James; an agent posts for its own person, who must be one of them). color green = On track, yellow = At risk, red = Blocked; text: one or two sentences. Saved as a GitHub comment whose first line is 'Status: <color>'.", 'inputSchema': {'type': 'object', 'properties': {**ISSUE, 'color': {'type': 'string', 'enum': ['green', 'yellow', 'red']}, 'text': {'type': 'string', 'minLength': 3, 'maxLength': 600}}, 'required': ['color', 'text']}},
    {'name': 'goal_status_get', 'description': "A goal's latest status (color, words, text, who, when, stale after 7 days) and its whole status history, newest first.", 'inputSchema': {'type': 'object', 'properties': ISSUE}},
    {'name': 'board_drop', 'description': "Drop a feature (not needed) or close a bug as not a bug / won't fix, only when a human on the team asked (pass their words). reason: their short reason (3-300 characters), saved in the comment with who said it. Closes it as not planned with label dropped (feature) or wontfix (bug); a feature already merged behind its switch also gets switch-cleanup, so the builder removes the switch and its code. Turn its Try it switch off first.", 'inputSchema': {'type': 'object', 'properties': {**ISSUE, 'reason': {'type': 'string', 'minLength': 3, 'maxLength': 300}, **ASKED}, 'required': ['reason', 'owner_request']}},
    {'name': 'board_reopen', 'description': 'Reopen a Bug when a human asked you to (pass their words). Goals are refused: only humans reopen goals.', 'inputSchema': {'type': 'object', 'properties': {**ISSUE, **ASKED}, 'required': ['owner_request']}},
]
CALLS = {
    'board_list': lambda a: board_list(None, a, agent=True),
    'board_get': lambda a: board_get(None, a, agent=True),
    'board_report_bug': lambda a: report_bug(None, a, agent=True),
    'board_report_feature': lambda a: report_feature(None, a, agent=True),
    'board_comment': lambda a: add_comment(None, a, agent=True),
    'report_or_request': lambda a: report_file(None, a, agent=True),
    'board_add_goal': lambda a: add_goal(None, a, agent=True),
    'board_close': lambda a: set_state(None, a, 'closed', agent=True),
    'board_reopen': lambda a: set_state(None, a, 'open', agent=True),
    'board_drop': lambda a: drop_item(None, a, agent=True),
    'builder_status': lambda a: builder_status(),
    'builder_set': lambda a: builder_set(None, a, agent=True),
    'try_status': lambda a: try_status(None, agent=True),
    'try_set': lambda a: try_set(None, a, agent=True),
    'board_flow': lambda a: item_flow(None, a, agent=True),
    'targets_set': lambda a: targets_set(None, a, agent=True),
    'ai_review_pass': lambda a: ai_review(a, True),
    'ai_review_fail': lambda a: ai_review(a, False),
    'goal_status_set': lambda a: goal_status_set(None, a, agent=True),
    'goal_status_get': lambda a: goal_status_get(None, a, agent=True),
}


def rpc(msg):
    if not isinstance(msg, dict):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Expected a JSON-RPC object.'}}
    m, i, p = msg.get('method'), msg.get('id'), msg.get('params') or {}
    if 'id' not in msg:
        return None
    try:
        if m == 'initialize':
            r = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': APP_ID, 'version': VERSION},
                 'instructions': AGENT_UPDATE + 'Tracker rules: agents may list, read, report bugs and comment. Never open, close or reopen goals on your own. A filed bug is ready to work. '
                                 'Line: Queued -> Building -> AI review (label ai-review) -> Human review (label ready-to-test) -> Done. '
                                 "Bugs and features: Works / Doesn't work by whoever filed them. Core features (repos in /api/policy core_repos) wait for approval by James or Miguel (label james-approved) before building, and James approves them again after a show-and-tell. "
                                 "After Works an item is Shipping until its merged change is in a release for everyone, then Done. Goals are human-led and never built: owners post status with goal_status_set; only James closes a goal. "
                                 "The builder hands over with ai-review; the opener's (or goal owner's) agent then calls ai_review_pass or ai_review_fail with a short note."}
        elif m == 'tools/list':
            r = {'tools': TOOLS}
        elif m == 'tools/call':
            a = p.get('arguments') or {}
            if not isinstance(a, dict):
                raise Problem(400, 'arguments must be an object.')
            fn = CALLS.get(p.get('name'))
            if not fn:
                return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32602, 'message': 'Unknown tool.'}}
            value = fn(a)
            r = {'content': [{'type': 'text', 'text': json.dumps(value)}], 'structuredContent': value, 'isError': False}
        elif m == 'ping':
            r = {}
        else:
            return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32601, 'message': 'Unknown method.'}}
    except Problem as e:
        r = {'content': [{'type': 'text', 'text': e.message}], 'isError': True}
    return {'jsonrpc': '2.0', 'id': i, 'result': r}


def _csv(name):
    return {x.strip().lower() for x in os.environ.get(name, '').split(',') if x.strip()}


# Who may talk to this app. Trust never comes from the request itself: the Host must be one this app
# was configured for (stops DNS rebinding), and a browser Origin on writes must be configured too.
_LOOPBACK = {f"127.0.0.1:{os.environ.get('PORT', '8799')}", f"localhost:{os.environ.get('PORT', '8799')}"}
# 0.12.1: in-network names (the app id and the old id) are always trusted, and loopback on any port (the host
# publishes the app on its own APP_PORT; health checks and the agent use 127.0.0.1:<APP_PORT>).
_IN_NETWORK = {f"{n}:{os.environ.get('PORT', '8799')}" for n in ('wizardos-tracker', 'wizard-os-tracker', 'board')}
_LOOPBACK_HOST = re.compile(r'^(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?$')
TRUSTED_HOSTS = _csv('WIZARD_TRUSTED_HOSTS') | _LOOPBACK | _IN_NETWORK
TRUSTED_ORIGINS = _csv('WIZARD_TRUSTED_ORIGINS') | {'http://' + h for h in _LOOPBACK}


class Handler(BaseHTTPRequestHandler):
    def refused(self, write=False):
        host = (self.headers.get('Host') or '').lower()
        if host not in TRUSTED_HOSTS and not _LOOPBACK_HOST.match(host):
            self.send(403, {'error': 'Unknown host.'})
            return True
        if write:
            origin = (self.headers.get('Origin') or '').lower()
            if origin and origin not in TRUSTED_ORIGINS:
                self.send(403, {'error': 'Cross-origin writes refused.'})
                return True
            if not origin and (self.headers.get('Sec-Fetch-Site') or '') == 'cross-site':
                self.send(403, {'error': 'Cross-origin writes refused.'})
                return True
        return False

    def person(self):
        # Each person signs in with their own token. Behind the Wizard gateway the tailnet login names them.
        return self.headers.get('Tailscale-User-Login') or ''

    def send(self, code, body=None, content_type='application/json'):
        b = body if isinstance(body, bytes) else b'' if body is None else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'self'")
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(b)

    def do_GET(self):
        if self.refused():
            return
        u = urlsplit(self.path)
        path, q = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}
        if path == '/healthz':
            return self.send(200, {'ok': True, 'app': APP_ID, 'version': VERSION})
        try:
            if path == '/api/status':
                return self.send(200, status(self.person()))
            if path == '/api/view':
                return self.send(200, board_view(self.person(), {**q, 'refresh': q.get('refresh') == '1'}))
            if path == '/api/detail':
                return self.send(200, issue_detail(self.person(), {**q, 'refresh': q.get('refresh') == '1'}))
            if path == '/api/split':
                return self.send(200, board_split(self.person(), {**q, 'refresh': q.get('refresh') == '1'}))
            if path == '/api/stats':
                return self.send(200, stats_view(self.person(), q))
            if path == '/api/flow':
                return self.send(200, item_flow(self.person(), {**q, 'refresh': q.get('refresh') == '1'}))
            if path == '/api/done':
                return self.send(200, done_list(self.person(), {**q, 'refresh': q.get('refresh') == '1'}))
            if path == '/api/builder':
                return self.send(200, builder_status(seen=q.get('from') == 'builder'))
            if path == '/api/try':
                return self.send(200, try_status(self.person()))
            if path == '/api/goal/status':
                return self.send(200, goal_status_get(self.person(), q))
            if path == '/api/policy':
                return self.send(200, policy(self.person()))
            if path == '/api/device':
                return self.send(200, device_status(self.person()))
            if path == '/api/settings/github-client-id':
                return self.send(200, client_id_info())
            if path == '/api/targets':
                return self.send(200, {'targets': policy_base()['targets'], 'note': 'Shared: the same targets for everyone. Permissions later.'})
            if path == '/api/board':
                return self.send(200, board_list(self.person(), {**q, 'refresh': q.get('refresh') == '1'}))
            if path == '/api/issue':
                return self.send(200, board_get(self.person(), q, fresh=q.get('fresh') == '1'))
            if path == '/api/report':
                return self.send(200, report_options(self.person()))
            if path == '/api/repos':
                return self.send(200, repos(self.person()))
            if path == '/api/boxes':
                return self.send(200, boxes(self.person()))
            if path == '/api/feedback/pending':
                return self.send(200, feedback_pending(self.person(), q))
            if path == '/api/avatar':
                data, kind = avatar(self.person())
                return self.send(200, data, kind)
        except Problem as e:
            return self.send(e.code, {'error': e.message, **({'reason': e.reason} if e.reason else {})})
        name = 'index.html' if path == '/' else path[1:]
        kind = STATIC_TYPES.get(Path(name).suffix)
        if kind and '/' not in name and not name.startswith('.') and (ROOT / 'web' / name).is_file():
            return self.send(200, (ROOT / 'web' / name).read_bytes(), kind)
        # 0.27: vendored, unmodified third-party files (html2canvas 1.4.1, MIT) live in web/dist
        sub = name[5:] if name.startswith('dist/') else ''
        if kind and sub and '/' not in sub and not sub.startswith('.') and (ROOT / 'web' / 'dist' / sub).is_file():
            return self.send(200, (ROOT / 'web' / 'dist' / sub).read_bytes(), kind)
        if path == '/openapi.json':
            return self.send(200, (ROOT / 'openapi.json').read_bytes())
        self.send(405 if path == '/mcp' else 404, {'error': 'Use POST /mcp.' if path == '/mcp' else 'Not found.'})

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if self.refused(write=True):
            return
        path = urlsplit(self.path).path
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.send(415, {'error': 'Use application/json.'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 <= size <= (SHOTS_BODY if path in ('/api/report', '/api/test/fails') else 65536):   # 0.24.2: screenshots ride along
                return self.send(413, {'error': 'Request too large.'})
            body = json.loads(self.rfile.read(size))
            if path == '/mcp':
                if not isinstance(body, dict):
                    return self.send(400, {'error': 'Expected a JSON-RPC object.'})
                out = rpc(body)
                return self.send(202 if out is None else 200, out)
            if not isinstance(body, dict):
                return self.send(400, {'error': 'Expected a JSON object.'})
            who = self.person()
            routes = {
                '/api/connect': lambda: connect(who, body),
                '/api/disconnect': lambda: disconnect(who),
                '/api/settings': lambda: set_org(body),
                '/api/goals': lambda: add_goal(who, body),
                '/api/bugs': lambda: report_bug(who, body),
                '/api/features': lambda: report_feature(who, body),
                '/api/issue/close': lambda: set_state(who, body, 'closed'),
                '/api/issue/reopen': lambda: set_state(who, body, 'open'),
                '/api/issue/task': lambda: set_task(who, body),
                '/api/issue/comment': lambda: add_comment(who, body),
                '/api/new': lambda: new_item(who, body),
                '/api/report': lambda: report_file(who, body),
                '/api/test/works': lambda: tested(who, body, True),
                '/api/test/fails': lambda: tested(who, body, False),
                '/api/goal/status': lambda: goal_status_set(who, body),
                '/api/james': lambda: james_decide(who, body),
                '/api/feedback/review': lambda: feedback_review(who, body),
                '/api/show': lambda: show_and_tell(who, body),
                '/api/drop': lambda: drop_item(who, body),
                '/api/onbox': lambda: on_box(who, body),
                '/api/builder': lambda: builder_set(who, body),
                '/api/try': lambda: try_set(who, body),
                '/api/targets': lambda: targets_set(who, body),
                '/api/device/start': lambda: device_start(who),
                '/api/device/cancel': lambda: device_cancel(who),
                '/api/settings/github-client-id': lambda: set_client_id(who, body),
                '/api/ai-review/pass': ai_review_human,
                '/api/ai-review/fail': ai_review_human,
            }
            if path not in routes:
                return self.send(404, {'error': 'Not found.'})
            if path in ('/api/ai-review/pass', '/api/ai-review/fail'):
                return routes[path]()
            if path not in ('/api/connect', '/api/disconnect', '/api/device/start', '/api/device/cancel'):
                gate(who)  # 0.12: signed in, a human login, an active team member (cached 10 min)
            return self.send(201 if path in ('/api/goals', '/api/bugs', '/api/features', '/api/new', '/api/report') else 200, routes[path]())
        except Problem as e:
            self.send(e.code, {'error': e.message, **({'reason': e.reason} if e.reason else {})})
        except (ValueError, TypeError, AttributeError, KeyError):
            self.send(400, {'error': 'Invalid request.'})

    def log_message(self, format, *args):
        pass

# ---- wizard.app-switches/1 ------------------------------------------------------------------------------------------
# Owner feature switches for the Tracker's Try it (wizard-os docs/APP_SWITCHES.md). Shared block: the same text ships in
# every Wizard app (template: wizard-app-manager templates/hello-wizard). Every new feature ships behind a switch, off by
# default: append it to SWITCHES with the issue it delivers, e.g.
#   {'id': 'compact-header', 'name': 'Compact top bar', 'description': '...', 'default': False,
#    'issues': ['humanitylabs-org/<repo>#20']}
# and gate the feature on switch_on('compact-header'). State lives on the box (DATA_DIR/switches.json), so the app's
# page, the Tracker (through the App Manager) and the agent see one state. GET/POST /api/switches, owner only: the
# box's Tailscale Serve owner login with X-Wizard-Switch: 1; never a bearer, agent grant or box-local host.
import json as _sw_json
import os as _sw_os
import re as _sw_re
import threading as _sw_threading
from pathlib import Path as _SwPath
from urllib.parse import urlsplit as _sw_urlsplit

SWITCHES = []
_SW_CONTRACT = 'wizard.app-switches/1'
_SW_ROUTE = _sw_re.compile(r'(?:/[a-z0-9][a-z0-9-]{0,62})?/api/switches/?')
_SW_LOCAL = {'localhost', '127.0.0.1', '::1', '[::1]', '0.0.0.0'}
_sw_lock = _sw_threading.Lock()


def _sw_file():
    return _SwPath(_sw_os.environ.get('WIZARD_SWITCHES_DIR') or _sw_os.environ.get('DATA_DIR') or '/data') / 'switches.json'


def _sw_saved():
    try:
        saved = _sw_json.loads(_sw_file().read_text())
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def switches_doc():
    saved = _sw_saved()
    return {'contract': _SW_CONTRACT, 'switches': [
        dict(s, on=saved[s['id']] if isinstance(saved.get(s['id']), bool) else bool(s.get('default'))) for s in SWITCHES]}


def switch_on(switch_id):
    return next((s['on'] for s in switches_doc()['switches'] if s['id'] == switch_id), False)


def _sw_set(switch_id, on):
    with _sw_lock:
        ids = {s['id'] for s in SWITCHES}
        saved = {k: v for k, v in _sw_saved().items() if k in ids and isinstance(v, bool)}
        saved[switch_id] = on
        f = _sw_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + '.tmp')
        tmp.write_text(_sw_json.dumps(saved))
        _sw_os.replace(tmp, f)
    return switches_doc()


def _sw_owner(h, write):
    hd = h.headers
    if hd.get('Authorization') or hd.get('X-Wizard-Switch') != '1':
        return False
    if (hd.get('Sec-Fetch-Site') or 'same-origin').lower() not in ('same-origin', 'none'):
        return False
    host = (hd.get('Host') or '').strip().lower()
    name = host.split(']')[0] + ']' if host.startswith('[') else host.rsplit(':', 1)[0]
    if not name or name in _SW_LOCAL or '.' not in name:      # box-local and container hosts never prove the owner
        return False
    origin = hd.get('Origin')
    trusted = {o.strip() for o in (_sw_os.environ.get('WIZARD_TRUSTED_ORIGINS') or '').split(',') if o.strip()}
    if origin and _sw_urlsplit(origin).netloc.lower() != host and origin not in trusted:
        return False
    if write and not origin:
        return False
    logins = [x.strip() for x in (hd.get_all('Tailscale-User-Login') or [])]
    owners = {x.strip().lower() for x in (_sw_os.environ.get('WIZARD_OWNER_LOGINS') or '').split(',') if x.strip()}
    return len(logins) == 1 and '@' in logins[0] and (not owners or logins[0].lower() in owners)


def _sw_send(h, code, doc):
    body = _sw_json.dumps(doc).encode()
    h.send_response(code)
    h.send_header('Content-Type', 'application/json')
    h.send_header('Cache-Control', 'no-store')
    h.send_header('Content-Length', str(len(body)))
    h.end_headers()
    if h.command != 'HEAD':
        h.wfile.write(body)


def _sw_handle(h):
    """Serve /api/switches (or /<app>/api/switches) and return True; False for every other request."""
    if not _SW_ROUTE.fullmatch(_sw_urlsplit(h.path).path):
        return False
    if h.command in ('GET', 'HEAD'):
        if not _sw_owner(h, False):
            _sw_send(h, 403, {'error': "Only this Wizard's owner can see these switches.", 'code': 'owner_only'})
        else:
            _sw_send(h, 200, switches_doc())
        return True
    if not _sw_owner(h, True):
        _sw_send(h, 403, {'error': "Only this Wizard's owner can change these switches.", 'code': 'owner_only'})
        return True
    try:
        if (h.headers.get('Content-Type') or '').split(';')[0].strip() != 'application/json':
            raise ValueError
        size = int(h.headers.get('Content-Length') or '0')
        if size <= 0 or size > 4096:
            raise ValueError
        body = _sw_json.loads(h.rfile.read(size))
        if not isinstance(body, dict) or not isinstance(body.get('id'), str) or not isinstance(body.get('on'), bool) \
                or set(body) - {'id', 'on'}:
            raise ValueError
    except (ValueError, TypeError):
        _sw_send(h, 400, {'error': 'Send JSON {"id": "...", "on": true|false}', 'code': 'invalid_input'})
        return True
    if body['id'] not in {s['id'] for s in SWITCHES}:
        _sw_send(h, 404, {'error': 'No such switch', 'code': 'unknown_switch'})
        return True
    try:
        _sw_send(h, 200, _sw_set(body['id'], body['on']))
    except OSError:
        _sw_send(h, 500, {'error': 'storage_failure'})
    return True


def wizard_switches_install(handler_cls):
    """Route the switch endpoint ahead of the app's own GET/HEAD/POST handlers."""
    for method in ('do_GET', 'do_HEAD', 'do_POST'):
        orig = getattr(handler_cls, method, None)
        if getattr(orig, '_wizard_switches', False):
            continue

        def wrapped(self, _orig=orig):
            if _sw_handle(self):
                return None
            if _orig is None:
                return self.send_error(501)
            return _orig(self)
        wrapped._wizard_switches = True
        setattr(handler_cls, method, wrapped)
    return handler_cls
# ---- end wizard.app-switches/1 ---------------------------------------------------------------------------------------

wizard_switches_install(Handler)


if __name__ == '__main__':
    secure_start()
    threading.Thread(target=refresh_loop, daemon=True).start()
    if os.environ.get('TRACKER_WARM', '1') != '0':
        threading.Thread(target=_warm_releases, daemon=True).start()
    ThreadingHTTPServer((os.environ.get('BIND', '0.0.0.0'), int(os.environ.get('PORT', '8799'))), Handler).serve_forever()
