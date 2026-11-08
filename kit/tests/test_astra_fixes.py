"""Regressions for the five Astra findings, using isolated local repositories."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from test_records import Fixture, PROFILE, STATE
from test_recovery import FeatureFixture, full_feature_cycle
from test_kit import fingerprint


class AstraFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.serial = 0

    def fixture(self, cls=Fixture):
        self.serial += 1
        return cls(Path(self.tmp.name) / str(self.serial))

    def check(self, f, expected, reason=None, literals=None):
        f.save()
        if literals:
            raw = f.state_path.read_text()
            for token, value in literals.items():
                raw = raw.replace(json.dumps(token), value)
            f.state_path.write_text(raw)
        before = fingerprint(f.root)
        r = f.check()
        self.assertEqual(fingerprint(f.root), before, 'check changed data/Git')
        self.assertEqual(r.returncode, expected, r.stdout + r.stderr)
        self.assertNotIn('Traceback', r.stderr)
        if reason:
            self.assertIn(reason, r.stdout)
        return r

    def test_01_common_effects_all_action_paths(self):
        for stage, effects, expected in [('prepare', ['read'], 0),
                ('implement', ['read', 'write', 'git'], 0),
                ('prepare', ['read', 'write', 'git'], 1),
                ('prepare', ['external'], 1)]:
            with self.subTest(stage=stage, effects=effects):
                f = self.fixture()
                a = f.work('A')
                a['assignment']['stage'] = stage
                a['attempts'][0]['effects'] = effects
                if 'external' in effects:
                    a['attempts'][0]['external_outcome'] = 'not-performed'
                self.check(f, expected)
                f.running('A')
                self.check(f, expected)
                f.submit('A')
                self.check(f, expected)
                f.accept('A')
                self.check(f, expected)

    def test_01_local_candidate_and_common_prepare_mandate(self):
        for common, local, effects, expected in [
                ('prepare', 'prepare', ['read'], 0),
                ('prepare', 'prepare', ['read', 'write', 'git'], 1),
                ('implement', 'implement', ['read', 'write', 'git'], 0)]:
            with self.subTest(common=common, effects=effects):
                f = self.fixture(FeatureFixture)
                parent = f.work('notes-feature')
                parent['assignment']['stage'] = common
                parent['assignment']['mandate']['stages'] = [common]
                task = f.local['works'][0]
                task['assignment']['stage'] = local
                task['attempts'][0]['effects'] = effects
                f.rebind_parent()
                self.check(f, expected)
                result = f.submit_local()
                self.check(f, expected)
                task.update(status='accepted', current_acceptance='local-ac', acceptances=[{
                    'id': 'local-ac', 'result': result['id'], 'result_revision': 1,
                    'assignment_revision': 1, 'by': 'notes-coordinator',
                    'evidence': ['notes-local-check'], 'unresolved': [], 'applicability': 'Saved local requirement'}])
                f.local['actions'] = [{'work': task['id'], 'kind': 'handoff', 'bases': []}]
                self.check(f, expected)

    def test_01_unused_history_readable_but_selected_effects_rejected(self):
        f = self.fixture()
        f.work('A')['assignment']['stage'] = 'prepare'
        f.submit('A')
        f.accept('A', handoff=False)
        f.add_work('D')
        self.check(f, 0)
        dep = {'work': 'A', 'acceptance': 'A-acceptance-1', 'criterion': 'A-criterion'}
        f.work('D')['assignment']['dependencies'] = [dep]
        f.action('D', 'assign', [{**dep, 'result': 'A-result-1', 'result_revision': 1, 'check': 'A-check-1'}])
        self.check(f, 1, 'подготовка')

    def basis(self, f, target='A', result='A-result-1', check='A-check-1'):
        dep = {'work': target, 'acceptance': target + '-acceptance-1', 'criterion': target + '-criterion'}
        f.add_work('D', dependencies=[dep])
        f.action('D', 'assign', [{**dep, 'result': result, 'result_revision': 1, 'check': check}])

    def test_02_selected_assignment_content_in_all_current_versions(self):
        for external in (False, True):
            for area in ('HEAD', 'index', 'working'):
                with self.subTest(external=external, area=area):
                    f = self.fixture()
                    f.submit('A'); f.accept('A', handoff=False)
                    self.basis(f)
                    original = copy.deepcopy(f.work('A')['assignment'])
                    path = f.root / 'assignment.json'
                    if external:
                        path.write_text(json.dumps(original))
                        f.work('A')['assignment'] = {'path': 'assignment.json'}
                    f.save()
                    f.commit('Registered current dependency')
                    self.check(f, 0)
                    changed = copy.deepcopy(original)
                    changed['goal'] = 'Different preserved requirement at the same revision'
                    if external:
                        path.write_text(json.dumps(changed))
                    else:
                        f.work('A')['assignment'] = changed
                    f.save()
                    if area == 'HEAD':
                        f.commit('Different normative text')
                    elif area == 'index':
                        f.git('add', '--', 'assignment.json' if external else STATE)
                    if area != 'working':
                        if external:
                            path.write_text(json.dumps(original))
                        else:
                            f.work('A')['assignment'] = original
                    self.check(f, 1, 'поручения')

    def test_02_r1_reconfirmation_must_preserve_exact_obligation(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                f = self.fixture()
                old = f.submit('A'); f.accept('A', handoff=False)
                original = copy.deepcopy(f.work('A')['assignment'])
                if changed:
                    f.work('A')['assignment']['criteria'][0]['expectation'] = 'A different behavior'
                f.add_work('B')
                new = f.submit('B', [{'work': 'A', 'assignment_revision': 1, 'criterion': 'A-criterion'}])
                f.accept('B', handoff=False)
                f.work('A')['assignment'] = original
                self.basis(f, result=new['id'], check='B-check-1')
                f.data['actions'][0]['bases'][0]['replacements'] = [{
                    'result': old['id'], 'revision': 1, 'check': 'A-check-1',
                    'reason': 'Explicitly recheck the exact accepted obligation', 'by': 'product-coordinator'}]
                f.commit('Current original requirement and selected confirmation')
                self.check(f, 1 if changed else 0, 'обязательства' if changed else None)

    def test_03_unused_authority_present_removed_restored_and_current_required(self):
        f = self.fixture()
        source = f.root / 'old-authority.md'
        source.write_text('Authority for the historical A only')
        a = f.work('A')['assignment']
        a['authority']['source'] = 'old-authority.md'; a['inputs'].append('old-authority.md')
        f.submit('A'); f.accept('A', handoff=False)
        self.basis(f)
        f.commit('Select old result')
        self.check(f, 0)
        f.work('D')['assignment'].pop('dependencies'); f.action('D', 'assign', [])
        self.check(f, 0)
        source.unlink(); f.commit('Archive unused authority')
        self.check(f, 0)
        source.write_text('Authority for the historical A only')
        self.check(f, 0)
        source.unlink()
        # A selected old result still needs its own current inputs.
        dep = {'work': 'A', 'acceptance': 'A-acceptance-1', 'criterion': 'A-criterion'}
        f.work('D')['assignment']['dependencies'] = [dep]
        f.action('D', 'assign', [{**dep, 'result': 'A-result-1', 'result_revision': 1, 'check': 'A-check-1'}])
        self.check(f, 1)
        f.work('D')['assignment'].pop('dependencies'); f.action('D', 'assign', [])
        f.work('D')['assignment']['authority']['source'] = 'missing-current.md'
        f.work('D')['assignment']['inputs'].append('missing-current.md')
        self.check(f, 1, 'отсутствует источник')

    def test_02_03_new_assignment_revision_keeps_old_acceptance_unchanged(self):
        f = self.fixture()
        f.submit('A'); f.accept('A', handoff=False)
        task = f.work('A')
        old_result = copy.deepcopy(task['attempts'][0]['result'])
        old_acceptances = copy.deepcopy(task['acceptances'])
        task['assignment'].update(revision=2, goal='A lawful next version of this work')
        task['attempts'].append({'id': 'A-attempt-2', 'assignment_revision': 2,
                                'executor': 'next', 'phase': 'assigned', 'effects': ['read']})
        task.update(status='ready', active_attempt='A-attempt-2')
        task.pop('current_acceptance')
        f.action('A', 'assign', [])
        self.check(f, 0)
        self.assertEqual(task['attempts'][0]['result'], old_result)
        self.assertEqual(task['acceptances'], old_acceptances)

    def reissue_confirmer(self, f):
        task = f.work('B')
        task['assignment'].update(revision=2, goal='Next B requirement; B1 remains accepted history')
        task.update(status='revise', blocker={'reason': 'Prepare next B revision', 'next_step': 'Inspect B2'})
        task.pop('current_acceptance')
        f.action('B', 'inspect')

    def test_02_selected_old_confirmer_survives_new_assignment(self):
        for area in ('working', 'HEAD', 'index'):
            with self.subTest(area=area):
                f = self.fixture(); f.r1(); f.save(); f.commit('Selected accepted B1')
                task = f.work('B')
                history = copy.deepcopy((task['attempts'], task['acceptances']))
                self.reissue_confirmer(f)
                if area == 'HEAD':
                    f.save(); f.commit('New B2; old B1 remains accepted')
                elif area == 'index':
                    f.save(); f.git('add', '--', STATE)
                self.check(f, 0)
                self.assertEqual((task['attempts'], task['acceptances']), history)

    def test_02_new_confirmer_revision_cannot_hide_same_pair_git_tamper(self):
        for area in ('HEAD', 'index'):
            with self.subTest(area=area):
                f = self.fixture(); f.r1(); f.save(); f.commit('Selected accepted B1')
                original = copy.deepcopy(f.work('B'))
                f.work('B')['assignment']['goal'] = 'Changed B1 content at the same revision'
                f.save()
                if area == 'HEAD':
                    f.commit('Tamper B1 in HEAD and index')
                else:
                    f.git('add', '--', STATE)
                f.data['works'][1] = original
                self.reissue_confirmer(f)
                self.check(f, 1, 'поручения отличается: ' + ('HEAD' if area == 'HEAD' else 'индекс'))

    def test_01_selected_history_requires_supported_stage_even_after_reissue(self):
        f = self.fixture()
        old = f.submit('A'); f.accept('A', handoff=False)
        f.add_work('B')
        f.work('B')['assignment']['stage'] = 'unknown-stage'
        result = f.submit('B', [{'work': 'A', 'assignment_revision': 1, 'criterion': 'A-criterion'}])
        f.accept('B', handoff=False)
        self.basis(f, result=result['id'], check='B-check-1')
        f.data['actions'][0]['bases'][0]['replacements'] = [{
            'result': old['id'], 'revision': 1, 'check': 'A-check-1',
            'reason': 'Explicit replacement', 'by': 'product-coordinator'}]
        f.save(); f.commit('Historical B1 with an unsupported stage')
        self.reissue_confirmer(f)
        f.work('B')['assignment']['stage'] = 'implement'
        self.check(f, 1, 'этап')

    def test_03_never_existing_historical_authority_rejected(self):
        f = self.fixture()
        a = f.work('A')['assignment']
        a['authority']['source'] = 'never-existed.md'; a['inputs'].append('never-existed.md')
        f.submit('A'); f.accept('A', handoff=False)
        f.add_work('D')
        self.check(f, 1)

    def test_02_03_new_confirmation_preserves_history_without_old_authority(self):
        f = self.fixture()
        source = f.root / 'old-authority.md'; source.write_text('Historical A authority')
        a = f.work('A')['assignment']
        a['authority']['source'] = 'old-authority.md'; a['inputs'].append('old-authority.md')
        old = f.submit('A'); f.accept('A', handoff=False)
        history = copy.deepcopy(f.work('A'))
        source.unlink()
        f.add_work('B')
        new = f.submit('B', [{'work': 'A', 'assignment_revision': 1, 'criterion': 'A-criterion'}])
        f.accept('B', handoff=False)
        self.basis(f, result=new['id'], check='B-check-1')
        f.data['actions'][0]['bases'][0]['replacements'] = [{
            'result': old['id'], 'revision': 1, 'check': 'A-check-1',
            'reason': 'New independent authority and exact accepted requirement', 'by': 'product-coordinator'}]
        f.commit('Save the new confirmation without rewriting A')
        self.assertEqual(f.work('A'), history)
        self.check(f, 0)

    def limit(self, f, scope=None):
        limit = {'id': 'quota', 'source': {'by': 'user', 'path': PROFILE},
                 'scope': scope or ['A'], 'unit': 'synthetic-units', 'cap': 10,
                 'remaining': 10, 'usage': [], 'allocations': []}
        f.data['real_limits'] = [limit]
        return limit

    def test_04_closed_feature_internal_and_direct_reserves_release(self):
        self.serial += 1
        root = (Path(self.tmp.name) / str(self.serial)).resolve()
        report = full_feature_cycle(root)
        # Reopen the generated data without constructing a second repository.
        f = object.__new__(FeatureFixture)
        f.root = root; f.state_path = root / STATE
        f.data = report['final_state']; f.local = report['final_local_state']
        limit = self.limit(f, ['notes-feature', 'product-integration'])
        attempt = next(w for w in f.local['works'] if w['id'] == 'notes-implementation')['active_attempt']
        limit.update(usage=[{'attempt': attempt, 'amount': 2}], remaining=8)
        f.data['usage_views'] = [{'limit': 'quota', 'attempts': [attempt]}]
        for allocations, expected in [([], 0), ([{'work': 'notes-feature', 'amount': 5}], 1),
                ([{'work': 'notes-implementation', 'amount': 5}], 1),
                ([{'work': 'notes-implementation', 'amount': 0}], 0),
                ([{'work': 'product-integration', 'amount': 8}], 0)]:
            with self.subTest(allocations=allocations):
                limit['allocations'] = allocations
                self.check(f, expected)
                self.assertEqual(limit['usage'], [{'attempt': attempt, 'amount': 2}])
                self.assertEqual(f.data['usage_views'][0]['attempts'], [attempt])

    def test_04_active_feature_may_reserve_accepted_internal_task(self):
        f = self.fixture(FeatureFixture)
        f.submit_local(); f.accept_local_and_assign_next()
        limit = self.limit(f, ['notes-feature'])
        limit['allocations'] = [{'work': 'notes-code', 'amount': 5}]
        self.check(f, 0)

    def test_05_literal_decimal_overcap_and_exact_boundary(self):
        f = self.fixture(); limit = self.limit(f)
        limit.update(cap=9007199254740992, remaining=0,
                     usage=[{'attempt': 'A-attempt-1', 'amount': 'VALUE'}])
        for literal, expected in [('9007199254740993.0', 1), ('9007199254740993', 1),
                                  ('9007199254740992', 0), ('9007199254740992.0', 0),
                                  ('9.007199254740992e15', 0)]:
            with self.subTest(literal=literal):
                self.check(f, expected, literals={'VALUE': literal})

    def test_05_all_quantity_fields_use_exact_decimal_values(self):
        f = self.fixture(); limit = self.limit(f)
        limit.update(cap='CAP', remaining='REMAINING',
                     usage=[{'attempt': 'A-attempt-1', 'amount': 'USAGE'}],
                     allocations=[{'work': 'A', 'amount': 'ALLOCATION'}])
        f.data['actions'].append({'work': 'A', 'kind': 'spend', 'bases': [], 'limit': 'quota', 'amount': 'SPEND'})
        cases = [
            ('9007199254740993.0', '0', '9007199254740993', '0', '0', 0),
            ('9007199254740993', '9007199254740993.0', '0', '0', '0', 0),
            ('9007199254740992', '9007199254740992', '0', '9007199254740993.0', '0', 1),
            ('9007199254740992', '9007199254740992', '0', '9007199254740992', '9007199254740993.0', 1),
            ('0.3', '2e-1', '0.10', '0.2', '0.2000', 0)]
        for cap, remaining, usage, allocation, spend, expected in cases:
            with self.subTest(cap=cap, remaining=remaining, allocation=allocation, spend=spend):
                self.check(f, expected, literals=dict(zip(
                    ['CAP', 'REMAINING', 'USAGE', 'ALLOCATION', 'SPEND'],
                    [cap, remaining, usage, allocation, spend])))

    def test_05_decimal_sums_and_invalid_numeric_forms(self):
        f = self.fixture(); limit = self.limit(f)
        f.add_work('B', status='blocked')
        # A stopped second attempt permits a distinct primary historical expense.
        f.work('A')['attempts'].append({'id': 'A-old', 'assignment_revision': 1, 'executor': 'old',
            'phase': 'stopped', 'effects': ['read'], 'termination': {'by': 'parent', 'fact': 'No process'}})
        limit['scope'].append('B')
        limit.update(cap=0.6, remaining=0.3,
            usage=[{'attempt': 'A-attempt-1', 'amount': 0.1}, {'attempt': 'A-old', 'amount': 0.2}],
            allocations=[{'work': 'A', 'amount': 0.1}, {'work': 'B', 'amount': 0.2}])
        self.check(f, 0)
        limit['allocations'][1]['amount'] = 0.20000000000000004
        self.check(f, 1)
        for field in ('cap', 'remaining', 'usage', 'allocation', 'spend'):
            for literal in ('-0.1', 'true', '"1"', 'NaN', 'Infinity', '-Infinity'):
                with self.subTest(field=field, literal=literal):
                    limit.update(cap=1, remaining=1, usage=[{'attempt': 'A-attempt-1', 'amount': 0}],
                                 allocations=[{'work': 'A', 'amount': 1}])
                    f.data['actions'] = [{'work': 'A', 'kind': 'assign', 'bases': []}]
                    if field in ('cap', 'remaining'):
                        limit[field] = 'BAD'
                    elif field == 'usage':
                        limit['usage'][0]['amount'] = 'BAD'
                    elif field == 'allocation':
                        limit['allocations'][0]['amount'] = 'BAD'
                    else:
                        f.data['actions'].append({'work': 'A', 'kind': 'spend', 'bases': [], 'limit': 'quota', 'amount': 'BAD'})
                    self.check(f, 1, literals={'BAD': literal})

    def test_05_parser_preserves_revision_types_and_optional_forecast(self):
        f = self.fixture()
        for bad in ('true', '1.0'):
            f.work('A')['assignment']['revision'] = 'BAD'
            self.check(f, 1, 'целым', literals={'BAD': bad})
        f.work('A')['assignment']['revision'] = 1
        f.data['forecast'] = {'estimate': 0.1, 'observed': None}
        self.check(f, 0)
        f.data['forecast']['observed'] = 0.2
        self.check(f, 0, 'прогноз превышен')


if __name__ == '__main__':
    unittest.main()
