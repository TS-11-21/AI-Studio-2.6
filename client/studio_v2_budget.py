"""Atomic line/role/member USD accounting. No providers or models are invoked."""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading

_LOCKS = {}
_GUARD = threading.Lock()
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
_ROLES = frozenset({'gameplay', 'vfx', 'integration', 'research'})


def amount(value, label='amount'):
    if isinstance(value, bool):
        raise ValueError(label+' must be a finite nonnegative USD amount.')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(label+' must be a finite nonnegative USD amount.') from None
    if not number.is_finite() or number < 0:
        raise ValueError(label+' must be a finite nonnegative USD amount.')
    return number


def _identifier(value, name):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(name+' must be a short nonsecret identifier.')
    return value


def _invocation(value):
    if not isinstance(value, str) or not value or len(value) > 2048 or not re.fullmatch(r'[A-Za-z0-9._/:\\-]+', value) or '://' in value:
        raise ValueError('Invocation must be a nonsecret local accounting identifier.')
    return value


class TeamBudgetLedger:
    def __init__(self, path, total, rolecaps, membercaps):
        candidate = Path(path).absolute()
        if candidate.resolve() != candidate:
            raise ValueError('A redirected budget ledger is not supported.')
        self.path = candidate
        self._configure_caps(total, rolecaps, membercaps)
        with _GUARD:
            self._lock = _LOCKS.setdefault(os.path.normcase(str(candidate)), threading.RLock())
        self._active = set()
        with self._lock:
            if candidate.exists():
                self._read()
            else:
                self._write({'schema': 'ai-studio-team-budget/v2', 'total_cap_usd': str(self.total_cap),
                    'role_caps': {role: str(cap) for role, cap in self.rolecaps.items()},
                    'member_caps': {member: {'role': data['role'], 'cap_usd': str(data['cap'])} for member, data in self.membercaps.items()},
                    'invocations': {}})

    def _configure_caps(self, total, rolecaps, membercaps):
        self.total_cap = amount(total, 'Line budget')
        if not isinstance(rolecaps, dict) or not rolecaps or set(rolecaps)-_ROLES:
            raise ValueError('Role caps must name known roles.')
        self.rolecaps = {role: amount(cap, 'Role budget') for role, cap in rolecaps.items()}
        self.membercaps = {}
        if not isinstance(membercaps, dict) or not membercaps:
            raise ValueError('Member caps must include their role and budget_usd.')
        for member, data in membercaps.items():
            _identifier(member, 'Member ID')
            if not isinstance(data, dict) or set(data) != {'role', 'budget_usd'} or data['role'] not in self.rolecaps:
                raise ValueError('Member caps must include exactly role and budget_usd.')
            cap = amount(data['budget_usd'], 'Member budget')
            if cap > self.rolecaps[data['role']]:
                raise ValueError('A member cap cannot exceed its shared role cap.')
            self.membercaps[member] = {'role': data['role'], 'cap': cap}
        if any(cap > self.total_cap for cap in self.rolecaps.values()):
            raise ValueError('A role cap cannot exceed its shared line cap.')

    def _read(self):
        if self.path.resolve() != self.path:
            raise RuntimeError('The ledger path became redirected; no payment is authorized.')
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            raise RuntimeError('Team budget ledger cannot be read; accounting cannot be reset.') from None
        if not isinstance(data, dict) or set(data) != {'schema', 'total_cap_usd', 'role_caps', 'member_caps', 'invocations'} or data['schema'] != 'ai-studio-team-budget/v2':
            raise RuntimeError('Unsupported or corrupt team budget ledger.')
        roles_match = isinstance(data['role_caps'], dict) and set(data['role_caps']) == set(self.rolecaps) and all(
            amount(data['role_caps'][role]) == cap for role, cap in self.rolecaps.items())
        members_match = isinstance(data['member_caps'], dict) and set(data['member_caps']) == set(self.membercaps) and all(
            isinstance(data['member_caps'][member], dict) and set(data['member_caps'][member]) == {'role', 'cap_usd'} and
            data['member_caps'][member]['role'] == item['role'] and amount(data['member_caps'][member]['cap_usd']) == item['cap']
            for member, item in self.membercaps.items())
        if amount(data['total_cap_usd']) != self.total_cap or not roles_match or not members_match:
            raise RuntimeError('Frozen line, role or member budgets differ from saved accounting; do not reset them.')
        if not isinstance(data['invocations'], dict):
            raise RuntimeError('Corrupt invocation accounting.')
        for key, entry in data['invocations'].items():
            _invocation(key)
            if not isinstance(entry, dict) or set(entry) != {'provider', 'role', 'member', 'state', 'reserved_cap_usd', 'cost_usd', 'source', 'metadata'}:
                raise RuntimeError('Corrupt hierarchical invocation entry.')
            _identifier(entry['provider'], 'Provider')
            self._member(entry['role'], entry['member'])
            if entry['state'] not in ('pending', 'reconciled', 'unknown') or entry['source'] not in ('reservation', 'legacy'):
                raise RuntimeError('Corrupt hierarchical accounting state.')
            amount(entry['reserved_cap_usd'])
            if entry['state'] == 'reconciled':
                amount(entry['cost_usd'])
            elif entry['cost_usd'] is not None:
                raise RuntimeError('Unproven accounting cannot contain a fabricated actual cost.')
            self._metadata(entry['metadata'])
        return data

    def _write(self, data):
        if self.path.resolve() != self.path:
            raise RuntimeError('A redirected team ledger cannot be written.')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', prefix='.'+self.path.name+'.', suffix='.tmp',
                                             dir=self.path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _member(self, role, member):
        if role not in self.rolecaps or member not in self.membercaps or self.membercaps[member]['role'] != role:
            raise ValueError('The selected member is not part of this frozen role.')
        return self.membercaps[member]

    def _metadata(self, metadata):
        if not isinstance(metadata, dict) or set(metadata)-{'original_invocation', 'native_usage_path', 'approval_cap_usd'}:
            raise ValueError('Only nonsecret accounting identity/path metadata may be stored.')
        if 'original_invocation' in metadata:
            _invocation(metadata['original_invocation'])
        path = metadata.get('native_usage_path')
        if path is not None and (not isinstance(path, str) or not path or len(path) > 32768 or any(ord(char) < 32 for char in path)):
            raise ValueError('Native usage path metadata is invalid.')
        if 'approval_cap_usd' in metadata:
            # Historical approvals remain evidence after an explicit cap
            # reduction. New reserve() still checks the current approval cap.
            amount(metadata['approval_cap_usd'])
        return dict(metadata)

    def reconfigure(self, total, rolecaps, membercaps, *, persist=True):
        """Explicit cap edit; invocation identities and costs are never reset.

        persist=False validates and returns the exact prospective ledger for
        the plan/ledger crash-recovery journal, without modifying this object.
        The normal constructor continues to reject mismatched saved caps.
        """
        if not isinstance(persist, bool):
            raise ValueError('persist must be true or false.')
        with self._lock:
            data = self._read()
            old = (self.total_cap, self.rolecaps, self.membercaps)
            state = self._snapshot(data, self._active)
            committed = False
            try:
                self._configure_caps(total, rolecaps, membercaps)
                if (set(self.rolecaps) != set(old[1]) or set(self.membercaps) != set(old[2]) or
                        any(item['role'] != old[2][member]['role'] for member, item in self.membercaps.items())):
                    raise ValueError('A budget edit cannot replace roles or members.')
                reduced = self.total_cap < old[0] or any(cap < old[1][role] for role, cap in self.rolecaps.items()) or any(
                    item['cap'] < old[2][member]['cap'] for member, item in self.membercaps.items())
                if reduced and any(entry['state'] == 'unknown' for entry in data['invocations'].values()):
                    raise RuntimeError('Unknown costs must be reconciled before lowering any budget.')
                layers = [(self.total_cap, state)] + [(cap, state['roles'][role]) for role, cap in self.rolecaps.items()] + [
                    (item['cap'], state['members'][member]) for member, item in self.membercaps.items()]
                if any(cap < amount(item['spent_usd'])+amount(item['reserved_usd']) for cap, item in layers):
                    raise RuntimeError('A budget cannot fall below its already spent and reserved amount.')
                target = {**data, 'total_cap_usd': str(self.total_cap),
                    'role_caps': {role: str(cap) for role, cap in self.rolecaps.items()},
                    'member_caps': {member: {'role': item['role'], 'cap_usd': str(item['cap'])} for member, item in self.membercaps.items()}}
                if persist:
                    self._write(target)
                    committed = True
                return target
            finally:
                if not committed:
                    self.total_cap, self.rolecaps, self.membercaps = old

    def _snapshot(self, data, active):
        def stats(cap):
            return {'cap_usd': str(cap), 'total_cap_usd': str(cap), 'spent_usd': Decimal(0), 'reserved_usd': Decimal(0),
                    'unknown_invocations': [], 'interrupted_invocations': []}
        total = stats(self.total_cap)
        roles = {role: stats(cap) for role, cap in self.rolecaps.items()}
        members = {member: {**stats(data['cap']), 'role': data['role']} for member, data in self.membercaps.items()}
        exceeded = []
        for key, entry in data['invocations'].items():
            targets = [total, roles[entry['role']], members[entry['member']]]
            if entry['state'] == 'reconciled':
                cost = amount(entry['cost_usd'])
                for item in targets:
                    item['spent_usd'] += cost
                if entry['source'] == 'reservation' and cost > amount(entry['reserved_cap_usd']):
                    exceeded.append(key)
            elif entry['state'] == 'pending':
                for item in targets:
                    item['reserved_usd'] += amount(entry['reserved_cap_usd'])
                    if key not in active:
                        item['interrupted_invocations'].append(key)
            else:
                for item in targets:
                    item['unknown_invocations'].append(key)
        for item in [total, *roles.values(), *members.values()]:
            remaining = amount(item['cap_usd'])-item['spent_usd']-item['reserved_usd']
            reasons = []
            if item['unknown_invocations']:
                reasons.append('Unknown costs require real supplier usage evidence.')
            if item['interrupted_invocations']:
                reasons.append('Unproven interrupted reservations require reconciliation.')
            if remaining <= 0:
                reasons.append('Budget has been spent or reserved.')
            item.update(spent_usd=str(item['spent_usd']), reserved_usd=str(item['reserved_usd']), remaining_usd=str(remaining),
                        available_usd=str(max(Decimal(0), remaining)) if not reasons else '0', blocked=bool(reasons), block_reasons=reasons,
                        costs_complete=not item['unknown_invocations'] and not item['interrupted_invocations'] and item['reserved_usd'] == Decimal(0))
        return {**total, 'schema': 'ai-studio-team-budget/v2', 'schema_version': 2, 'roles': roles, 'members': members,
                'invocations': data['invocations'], 'reservation_exceeded_invocations': exceeded,
                'cost_reconciliation_required': bool(total['unknown_invocations'] or total['interrupted_invocations']),
                'billing_limit_note': 'Local CLI/provider estimates and reservations; final supplier charges may exceed these caps.'}

    def snapshot(self, *, active_invocations=None):
        """Read-only report; GUI liveness hints never influence reserve authorization."""
        with self._lock:
            active = self._active if active_invocations is None else set(active_invocations)
            return self._snapshot(self._read(), active)

    def reserve(self, invocation, provider, role, member, requested, *, metadata=None, approval_cap=None):
        _invocation(invocation)
        _identifier(provider, 'Provider')
        self._member(role, member)
        requested = amount(requested, 'Requested invocation budget')
        if requested <= 0:
            raise RuntimeError('A paid invocation requires a positive requested budget.')
        metadata = self._metadata(metadata or {})
        approval_cap = self.total_cap if approval_cap is None else amount(approval_cap, 'Approved cumulative line cap')
        if approval_cap > self.total_cap:
            raise ValueError('The approval cannot exceed the frozen line budget.')
        metadata['approval_cap_usd'] = str(approval_cap)
        with self._lock:
            data = self._read()
            if invocation in data['invocations']:
                raise RuntimeError('Invocation already accounted; its budget cannot be reset.')
            state = self._snapshot(data, self._active)
            if state['unknown_invocations'] or state['interrupted_invocations']:
                raise RuntimeError('Unknown costs or interrupted reservations block all further paid work on this line.')
            candidates = [state, state['roles'][role], state['members'][member]]
            if any(item['blocked'] for item in candidates):
                raise RuntimeError('Line, role or member budget is exhausted or reserved.')
            approved_remaining = approval_cap-amount(state['spent_usd'])-amount(state['reserved_usd'])
            if approved_remaining <= 0:
                raise RuntimeError('The approved cumulative line budget is exhausted or reserved.')
            granted = min(requested, approved_remaining, *(amount(item['available_usd']) for item in candidates))
            data['invocations'][invocation] = {'provider': provider, 'role': role, 'member': member, 'state': 'pending',
                'reserved_cap_usd': str(granted), 'cost_usd': None, 'source': 'reservation', 'metadata': metadata}
            self._write(data)
            self._active.add(invocation)
            return granted

    def reconcile(self, invocation, cost):
        _invocation(invocation)
        cost = None if cost is None else amount(cost, 'Supplier-reported cost')
        with self._lock:
            data = self._read()
            entry = data['invocations'].get(invocation)
            if entry is None:
                raise RuntimeError('A cost cannot be reconciled without its original invocation record.')
            if entry['state'] == 'reconciled':
                if cost != amount(entry['cost_usd']):
                    raise RuntimeError('Known costs cannot be overwritten or reset.')
                return
            self._active.discard(invocation)
            entry.update(state='unknown' if cost is None else 'reconciled', cost_usd=None if cost is None else str(cost))
            self._write(data)

    def import_cost(self, invocation, cost, *, role, member, provider='legacy', metadata=None):
        _invocation(invocation)
        _identifier(provider, 'Provider')
        self._member(role, member)
        cost = None if cost is None else amount(cost, 'Imported recorded cost')
        with self._lock:
            data = self._read()
            if invocation in data['invocations']:
                entry = data['invocations'][invocation]
                same_cost = entry['cost_usd'] is None and cost is None or entry['cost_usd'] is not None and cost is not None and amount(entry['cost_usd']) == cost
                if entry['source'] != 'legacy' or entry['role'] != role or entry['member'] != member or not same_cost:
                    raise RuntimeError('Imported accounting identity/cost conflicts with saved evidence.')
                return
            data['invocations'][invocation] = {'provider': provider, 'role': role, 'member': member,
                'state': 'unknown' if cost is None else 'reconciled', 'reserved_cap_usd': '0',
                'cost_usd': None if cost is None else str(cost), 'source': 'legacy', 'metadata': self._metadata(metadata or {})}
            self._write(data)

    def bind(self, role, member, workspace_root=None, *, approval_cap=None):
        self._member(role, member)
        return TeamBudgetView(self, role, member, workspace_root, approval_cap=approval_cap)


class TeamBudgetView:
    def __init__(self, ledger, role, member, workspace_root=None, *, approval_cap=None):
        self.ledger = ledger
        self.role = role
        self.member = member
        self.approval_cap = ledger.total_cap if approval_cap is None else amount(approval_cap)
        if self.approval_cap > ledger.total_cap:
            raise ValueError('The approval cannot exceed the frozen line budget.')
        self.workspace_root = Path(workspace_root).absolute() if workspace_root is not None else None
        if self.workspace_root is not None and self.workspace_root.resolve() != self.workspace_root:
            raise ValueError('Member workspace accounting path must not be redirected.')

    def key_for(self, invocation):
        _invocation(invocation)
        return 'v2:'+self.role+':'+self.member+':'+hashlib.sha256(invocation.encode()).hexdigest()[:32]

    def _metadata(self, invocation):
        output = {'original_invocation': invocation}
        if invocation.startswith('native:') and self.workspace_root is not None:
            relative = invocation.split(':', 1)[1].replace('\\', '/')
            if ':' in relative or relative.startswith('/') or any(part in ('', '.', '..') for part in relative.split('/')):
                raise ValueError('Native usage path must stay in its member workspace.')
            path = self.workspace_root/relative
            if path.resolve() != path.absolute() or not path.is_relative_to(self.workspace_root):
                raise ValueError('Native usage path must stay in its member workspace.')
            output['native_usage_path'] = str(path)
        return output

    def reserve(self, invocation, provider, requested):
        return self.ledger.reserve(self.key_for(invocation), provider, self.role, self.member, requested,
                                   metadata=self._metadata(invocation), approval_cap=self.approval_cap)

    def reconcile(self, invocation, cost):
        return self.ledger.reconcile(self.key_for(invocation), cost)

    def import_cost(self, invocation, cost):
        return self.ledger.import_cost(self.key_for(invocation), cost, role=self.role, member=self.member, metadata=self._metadata(invocation))

    def snapshot(self):
        state = self.ledger.snapshot()
        role = state['roles'][self.role]
        member = state['members'][self.member]
        available = min(amount(item['available_usd']) for item in (state, role, member))
        remaining = self.approval_cap-amount(state['spent_usd'])-amount(state['reserved_usd'])
        available = min(available, max(Decimal(0), remaining))
        return {**state, 'available_usd': str(available), 'blocked': state['blocked'] or role['blocked'] or member['blocked'] or remaining <= 0,
                'approved_cap_usd': str(self.approval_cap), 'approved_remaining_usd': str(remaining),
                'bound_role': self.role, 'bound_member': self.member, 'role_budget': role, 'member_budget': member}
