"""Package-2 Git scopes, external sources, compatibility and numeric limits."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_kit import TOOL, KIT, Fixture as LegacyFixture, environment, fingerprint
from test_records import Fixture, PROFILE, STATE
from test_records import BASE_CODE, NOTE_CODE, BASE_TESTS, NOTE_TESTS


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='kit-p2-diagnostics-')
        self.addCleanup(self.tmp.cleanup)
        self.f = Fixture(Path(self.tmp.name) / 'repo')

    def check(self, code=0, text='', env=None):
        before = fingerprint(self.f.root)
        run = subprocess.run([sys.executable, '-B', str(TOOL), 'check', '--root', str(self.f.root)],
                             env=env or environment(), capture_output=True, text=True)
        self.assertEqual(run.returncode, code, run.stdout + run.stderr)
        self.assertIn(text, run.stdout)
        self.assertNotIn('Traceback', run.stdout + run.stderr)
        self.assertEqual(before, fingerprint(self.f.root))
        return run

    def accepted(self):
        self.f.submit('A')
        self.f.accept('A')

    def test_folder_membership_at_head_index_and_working_tree(self):
        for mode in ('untracked', 'staged', 'committed'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as parent:
                self.f = Fixture(Path(parent) / 'repo')
                self.accepted()
                extra = self.f.root / 'code/new_module.py'
                extra.write_text('NEW = 1\n')
                if mode == 'staged':
                    self.f.git('add', '--', 'code/new_module.py')
                elif mode == 'committed':
                    self.f.commit('New input after the check')
                self.check(1, 'индекс' if mode == 'staged' else 'вход')

    def test_distinct_head_index_working_content_and_mode(self):
        for mode in ('working', 'index', 'HEAD', 'executable'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as parent:
                self.f = Fixture(Path(parent) / 'repo')
                self.accepted()
                source = self.f.root / 'code/source.py'
                original = source.read_bytes()
                if mode == 'executable':
                    source.chmod(source.stat().st_mode | 0o100)
                else:
                    source.write_bytes(original + b'\n# changed\n')
                    if mode == 'index':
                        self.f.git('add', '--', 'code/source.py')
                        source.write_bytes(original)
                    elif mode == 'HEAD':
                        self.f.commit('Changed inputs')
                        source.write_bytes(original)
                self.check(1, {'working': 'рабочий вход', 'index': 'индекс', 'HEAD': 'HEAD', 'executable': 'права'}[mode])

    def test_ignored_data_not_claimed_and_report_only_commit_allowed(self):
        self.accepted()
        (self.f.root / '.gitignore').write_text('code/private-data\n')
        (self.f.root / 'code/private-data').write_text('synthetic ignored data')
        self.f.commit('Only report and ignore policy outside registered inputs')
        self.check()
        self.assertNotIn('code/private-data', self.f.git('ls-files'))

    def test_merge_commit_with_identical_inputs_stays_applicable(self):
        self.accepted()
        self.f.commit('Report after accepted check')
        self.f.git('checkout', '--quiet', '-b', 'topic')
        (self.f.root / 'topic-report.md').write_text('topic facts\n')
        self.f.commit('Topic report outside product inputs')
        self.f.git('checkout', '--quiet', 'main')
        (self.f.root / 'main-report.md').write_text('main facts\n')
        self.f.commit('Main report outside product inputs')
        self.f.git('merge', '--quiet', '--no-ff', 'topic', '-m', 'Merge reports')
        self.assertNotEqual(self.f.git('rev-parse', 'HEAD'), self.f.work('A')['attempts'][0]['result']['commit'])
        self.check()
        (self.f.root / 'code/source.py').write_text('broken after merge\n')
        self.check(1, 'рабочий вход')

    def test_git_commit_of_one_work_preserves_unrelated_staged_change(self):
        neighbor = self.f.root / 'neighbor.txt'
        neighbor.write_text('previous unrelated version\n')
        self.f.commit('Unrelated existing file')
        neighbor.write_text('other work staged version\n')
        self.f.git('add', '--', 'neighbor.txt')
        source = self.f.root / 'code/source.py'
        source.write_text(source.read_text() + '\n# This work only.\n')
        self.f.git('commit', '--quiet', '--only', '-m', 'Only this work', '--', 'code/source.py')
        self.assertEqual(self.f.git('show', 'HEAD:neighbor.txt'), 'previous unrelated version')
        self.assertEqual(self.f.git('show', ':neighbor.txt'), 'other work staged version')
        self.assertIn('This work only', self.f.git('show', 'HEAD:code/source.py'))
        self.check()

    def test_dependent_candidate_rechecks_dependency_before_own_acceptance(self):
        old = self.f.submit('A')
        self.f.accept('A', handoff=False)
        dep = {'work': 'A', 'acceptance': 'A-acceptance-1', 'criterion': 'A-criterion'}
        self.f.add_work('B', dependencies=[dep])
        (self.f.root / 'code/source.py').write_text(BASE_CODE + NOTE_CODE)
        (self.f.root / 'code/test_source.py').write_text(BASE_TESTS + NOTE_TESTS)
        candidate = self.f.submit('B', [{'work': 'A', 'assignment_revision': 1, 'criterion': 'A-criterion'}])
        basis = {**dep, 'result': candidate['id'], 'result_revision': 1, 'check': 'B-check-1',
                 'replacements': [{'result': old['id'], 'revision': 1, 'check': 'A-check-1',
                                   'reason': 'This candidate rechecks storage after its own change', 'by': 'product-coordinator'}]}
        self.f.action('B', 'review', [basis])
        self.f.save()
        self.assertNotIn('acceptances', self.f.work('B'))
        self.check()
        # An unaccepted candidate cannot authorize a separate following assignment.
        self.f.work('B')['status'] = 'paused'
        self.f.work('B')['blocker'] = {'reason': 'Review pending', 'next_step': 'Finish review'}
        self.f.action('B', 'inspect')
        self.f.add_work('D', dependencies=[dep])
        self.f.action('D', 'assign', [basis])
        self.f.save()
        self.check(1, 'ещё не принято')

    def test_compatibility_is_bound_to_exact_pair_and_obligation(self):
        _, old = self.f.r1()
        source = self.f.root / 'code/source.py'
        source.write_text(source.read_text() + '\n# Cosmetic explanation only.\n')
        target = self.f.commit('Comment only; no executable statement changed')
        basis = self.f.data['actions'][0]['bases'][0]
        basis['compatibility'] = {
            'from_commit': old['commit'], 'to_commit': target, 'work': 'A',
            'assignment_revision': 1, 'criterion': 'A-criterion',
            'differences': ['code/source.py'], 'reason': 'Only a comment was appended; persistence statements and tests are identical',
            'by': 'product-coordinator'}
        self.f.save()
        self.check()
        original = copy.deepcopy(basis['compatibility'])
        for key, value in [('from_commit', target), ('to_commit', old['commit']),
                           ('work', 'B'), ('criterion', 'B-criterion'), ('assignment_revision', True),
                           ('differences', ['code/test_source.py']), ('reason', '')]:
            with self.subTest(key=key):
                basis['compatibility'] = {**original, key: value}
                self.f.save()
                self.check(1)
        basis['compatibility'] = original
        source.write_text(source.read_text() + 'print("new behavior")\n')
        self.f.save()
        self.check(1, 'рабочий вход')

    def test_external_assignment_and_result_have_single_source(self):
        a = self.f.work('A')['assignment']
        assignment_file = self.f.root / 'docs/ai-work/assignment.json'
        assignment_file.write_text(json.dumps(a))
        self.f.work('A')['assignment'] = {'path': 'docs/ai-work/assignment.json'}
        self.f.save()
        # Fixture.submit reads assignment fields; keep the checked source external,
        # then create a real result by temporarily using the same in-memory object.
        checked = self.f.commit('External assignment source')
        del checked
        self.check()
        self.f.work('A')['assignment'] = a
        result = self.f.submit('A')
        self.f.accept('A')
        # The snapshot uses the inline source at the checked commit. A path ref to
        # identical content is valid at HEAD/index and in the working ledger.
        self.f.work('A')['assignment'] = {'path': 'docs/ai-work/assignment.json'}
        result_file = self.f.root / 'docs/ai-work/result.json'
        result_file.write_text(json.dumps(result))
        self.f.work('A')['attempts'][0]['result'] = {'path': 'docs/ai-work/result.json'}
        self.f.save()
        self.f.commit('External sources; no changed requirements or code')
        self.check()
        a['goal'] += ' new requirement with old revision'
        assignment_file.write_text(json.dumps(a))
        self.check(1, 'поручения')

    def test_new_source_paths_reject_escape_missing_symlink_url_and_duplicates(self):
        self.accepted()
        original = copy.deepcopy(self.f.data)
        outside = Path(self.tmp.name) / 'outside.json'
        outside.write_text('PRIVATE_SENTINEL_DO_NOT_READ')
        (self.f.root / 'escape.json').symlink_to(outside)
        cases = [({'path': '../outside.json'}, 1), ({'path': str(outside)}, 1),
                 ({'path': 'escape.json'}, 1), ({'path': 'missing.json'}, 1),
                 ({'url': 'https://example.invalid/result'}, 2)]
        for value, code in cases:
            with self.subTest(value=value):
                self.f.data = copy.deepcopy(original)
                self.f.work('A')['attempts'][0]['result'] = value
                self.f.save()
                run = self.check(code)
                self.assertNotIn('PRIVATE_SENTINEL', run.stdout + run.stderr)
        self.f.data = copy.deepcopy(original)
        source = self.f.root / 'shared-assignment.json'
        source.write_text(json.dumps(self.f.work('A')['assignment']))
        self.f.work('A')['assignment'] = {'path': 'shared-assignment.json'}
        self.f.add_work('future', status='blocked')['assignment'] = {'path': 'shared-assignment.json'}
        self.f.save()
        self.check(1, 'повтор действующего источника')

    def test_nested_sources_json_unicode_and_unknown_versions(self):
        self.accepted()
        original = copy.deepcopy(self.f.data)
        result_file = self.f.root / 'result.json'
        result = copy.deepcopy(self.f.work('A')['attempts'][0]['result'])
        self.f.work('A')['attempts'][0]['result'] = {'path': 'result.json'}
        self.f.save()
        for raw, code, text in [('{', 1, 'нечитаемый JSON'),
                                ('{"a":1,"a":2}', 1, 'повтор ключа'),
                                (json.dumps({**result, '\ud800': 1}), 2, '\\ud800')]:
            with self.subTest(raw=raw[:20]):
                result_file.write_text(raw)
                env = environment()
                env['PYTHONIOENCODING'] = 'utf-8:strict'
                self.check(code, text, env)
        self.f.data = original
        self.f.data['format_version'] = '0.1-p999'
        self.f.save()
        self.check(2, 'format_version')

    def test_folder_symlinks_and_required_source_are_read_only_errors(self):
        self.accepted()
        outside = Path(self.tmp.name) / 'outside.txt'
        outside.write_text('PRIVATE_SENTINEL')
        (self.f.root / 'code/link').symlink_to(outside)
        self.check(1, 'символические ссылки')
        (self.f.root / 'code/link').unlink()
        (self.f.root / PROFILE).unlink()
        self.check(1, 'отсутствует источник')

    def test_git_safety_and_injected_environment_on_new_read_routes(self):
        self.accepted()
        outside = Path(self.tmp.name) / 'outside-config'
        outside.write_text('PRIVATE INVALID CONFIG')
        config = self.f.root / '.git/config'
        original = config.read_bytes()
        additions = ['\n[include]\npath = ' + str(outside) + '\n',
                     '\n[includeIf "gitdir:*/"]\npath = ' + str(outside) + '\n',
                     '\n[extensions]\nworktreeConfig=true\n',
                     '\n[remote "origin"]\npromisor=true\n']
        for suffix in additions:
            config.write_bytes(original + suffix.encode())
            self.check(2, '.git/config')
        config.write_bytes(original + b'\n[remote "include.extensions"]\nurl=https://example.invalid/worktree\n')
        env = environment()
        env.update(GIT_CONFIG_GLOBAL=str(outside), GIT_CONFIG_SYSTEM=str(outside), GIT_DIR=str(outside),
                   GIT_OBJECT_DIRECTORY=str(outside), GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='include.path',
                   GIT_CONFIG_VALUE_0=str(outside), GIT_TRACE=str(self.f.root / 'TRACE_WAS_WRITTEN'))
        self.check(env=env)
        config.write_bytes(original + b'\n[diff]\nexternal=touch HANDLER\n[core]\nfsmonitor=touch HANDLER\n[filter "x"]\nprocess=touch HANDLER\n')
        (self.f.root / '.gitattributes').write_text('*.py filter=x diff=x\n')
        self.check()
        self.assertFalse((self.f.root / 'HANDLER').exists())
        self.assertFalse((self.f.root / 'TRACE_WAS_WRITTEN').exists())
        for name in ('objects/info/alternates', 'commondir', 'info/grafts'):
            p = self.f.root / '.git' / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(outside))
            self.check(2, 'хранилище Git')
            p.unlink()

    def limit(self, remaining=7):
        self.f.data['real_limits'] = [{
            'id': 'quota', 'source': {'by': 'user', 'path': PROFILE}, 'scope': ['A'],
            'unit': 'synthetic-units', 'cap': 10, 'remaining': remaining,
            'usage': [{'attempt': 'A-attempt-1', 'amount': 3}],
            'allocations': [{'work': 'A', 'amount': 4}]}]
        return self.f.data['real_limits'][0]

    def test_limits_known_unknown_double_allocation_and_no_double_summary(self):
        self.accepted()
        self.limit()
        self.f.data['usage_views'] = [{'limit': 'quota', 'attempts': ['A-attempt-1']}]
        self.f.data['actions'].append({'work': 'A', 'kind': 'spend', 'bases': [], 'limit': 'quota', 'amount': 4})
        self.f.save()
        self.check()
        limit = self.f.data['real_limits'][0]
        original = copy.deepcopy(self.f.data)
        for change, text in [('exceeded', 'превышено'), ('double', 'повтор'), ('unknown', 'остаток неизвестен'),
                             ('too-much', 'превышает'), ('wrong-source', 'источник'), ('wrong-unit', 'строка'),
                             ('summary-amount', 'неизвестные поля')]:
            with self.subTest(change=change):
                self.f.data = copy.deepcopy(original)
                limit = self.f.data['real_limits'][0]
                if change == 'exceeded':
                    limit['usage'][0]['amount'] = 11
                elif change == 'double':
                    limit['allocations'].append({'work': 'A', 'amount': 4})
                elif change == 'unknown':
                    limit['remaining'] = None
                    limit['usage'][0]['amount'] = None
                elif change == 'too-much':
                    self.f.data['actions'][-1]['amount'] = 5
                elif change == 'wrong-source':
                    limit['source']['path'] = 'missing.md'
                elif change == 'wrong-unit':
                    limit['unit'] = ''
                else:
                    self.f.data['usage_views'][0]['amount'] = 3
                self.f.save()
                self.check(2 if change == 'summary-amount' else 1, text)
        self.f.data = copy.deepcopy(original)
        limit = self.f.data['real_limits'][0]
        limit.update(remaining=None, usage=[{'attempt': 'A-attempt-1', 'amount': None}])
        self.f.data['actions'] = [{'work': 'A', 'kind': 'inspect'}]
        self.f.data['forecast'] = {'estimate': 1, 'observed': None}
        self.f.save()
        self.check(0, 'только чтение')

    def test_allocations_across_works_share_one_remaining_pool(self):
        self.accepted()
        self.f.add_work('B', status='blocked')
        limit = self.limit()
        limit['scope'].append('B')
        limit['allocations'].append({'work': 'B', 'amount': 4})
        self.f.save()
        self.check(1, 'общий остаток')
        limit['allocations'][-1]['amount'] = 3
        self.f.save()
        self.check()

    def test_forecast_missing_exceeded_and_unknown_are_informational(self):
        self.check()
        for observation in (None, 9):
            self.f.data['forecast'] = {'estimate': 1, 'observed': observation,
                                      'analysis': 'Known progress: assignment ready. Remaining: implement and test. Continue within the same scope.'}
            self.f.save()
            run = self.check()
            if observation == 9:
                self.assertIn('прогноз превышен', run.stdout)

    def test_limit_decimal_arithmetic_and_duplicate_primary_usage(self):
        self.accepted()
        limit = self.limit()
        limit.update(cap=0.3, remaining=0.2, usage=[{'attempt': 'A-attempt-1', 'amount': 0.1}],
                     allocations=[{'work': 'A', 'amount': 0.2}])
        self.f.data['actions'].append({'work': 'A', 'kind': 'spend', 'bases': [], 'limit': 'quota', 'amount': 0.2})
        self.f.save()
        self.check()
        limit['usage'].append({'attempt': 'A-attempt-1', 'amount': 0.1})
        self.f.save()
        self.check(1, 'повторная расходная попытка')

    def test_general_revision_does_not_rewrite_historical_acceptance(self):
        decision_path = 'docs/ai-work/decision.md'
        (self.f.root / decision_path).write_text('Decision one\n')
        self.f.data['decisions'] = [{'id': 'decision', 'revision': 1, 'source': decision_path}]
        self.f.work('A')['assignment']['decisions'] = [{'id': 'decision', 'revision': 1}]
        self.f.work('A')['assignment']['inputs'].append(decision_path)
        self.f.submit('A')
        self.f.accept('A', handoff=False)
        (self.f.root / decision_path).write_text('Decision two\n')
        self.f.data['decisions'][0]['revision'] = 2
        future = self.f.add_work('future', status='revise')
        future['assignment']['decisions'] = [{'id': 'decision', 'revision': 1}]
        future['assignment']['inputs'].append(decision_path)
        self.f.action('future', 'inspect')
        self.f.data['revision_review'] = {'decision': 'decision', 'revision': 2,
                                          'owner': 'product-coordinator', 'pending': ['future'],
                                          'updated': [], 'independent_actions': ['Read the changed norm; A remains history']}
        self.f.save()
        self.check(0, 'только чтение')

    def test_feature_mandate_detects_changed_normative_source_with_old_revision(self):
        from test_recovery import FeatureFixture
        self.f = FeatureFixture(Path(self.tmp.name) / 'feature')
        self.check()
        profile = self.f.root / PROFILE
        profile.write_text(profile.read_text() + '\nChanged normative authority without rebinding the mandate.\n')
        self.check(1, '/parent/norms')

    def test_clean_public_state_has_no_synthetic_assignment_or_writer(self):
        self.f.data = json.loads((KIT / 'templates/state.json').read_text())
        self.f.save()
        self.check()
        self.assertEqual(self.f.data['works'], [])
        self.assertEqual(self.f.data['actions'], [])
        self.assertNotIn('write_owner', self.f.data)
        self.assertNotIn('git_owner', self.f.data)

    def test_public_forms_support_actual_small_task_and_candidate(self):
        self.f = LegacyFixture(Path(self.tmp.name) / 'public-forms')
        self.f.data = json.loads((KIT / 'examples/single-task.state.json').read_text())
        self.f.save()
        self.check()

        task = self.f.data['works'][0]
        attempt = task['attempts'][0]
        task['status'] = 'in_progress'
        attempt['phase'] = 'running'
        self.f.data['actions'][0]['kind'] = 'execute'
        (self.f.root / 'source.py').write_text("def normalize(value):\n    return ' '.join(value.split())\n")
        self.f.save()
        checked = self.f.commit('Public p2 forms and meaningful source fix before test')
        run = subprocess.run([sys.executable, '-B', '-m', 'unittest', '-v', 'test_source'],
                             cwd=self.f.root, env=environment(), capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        result = json.loads((KIT / 'templates/result.json').read_text())
        result.update(commit=checked, summary='Public form: split/join fixes grouped spaces')
        result['checks'][0].update(observed=run.stdout + run.stderr, environment='Actual local Python subprocess')
        attempt.update(phase='finished', termination={'by': 'test-parent', 'fact': 'subprocess.run returned 0 after two actual normalization tests'}, result=result)
        task['status'] = 'submitted'
        self.f.data['actions'][0]['kind'] = 'review'
        self.f.save()
        self.check()

    def test_hidden_assignment_in_head_and_index_cannot_be_masked(self):
        for place in ('HEAD', 'индекс'):
            with self.subTest(place=place), tempfile.TemporaryDirectory() as parent:
                self.f = Fixture(Path(parent) / 'repo')
                self.f.submit('A')
                tested = copy.deepcopy(self.f.data)
                self.f.work('A')['assignment']['goal'] += ' hidden new requirement'
                self.f.save()
                if place == 'HEAD':
                    self.f.commit('Changed requirement with old revision')
                else:
                    self.f.git('add', '--', STATE)
                self.f.data = tested
                self.f.save()
                self.check(1, place)

    def test_malformed_reference_types_return_readable_errors(self):
        self.f.r1()
        original = copy.deepcopy(self.f.data)
        changes = [lambda f: f.data['actions'][0].update(work=[]),
                   lambda f: f.data['actions'][0]['bases'][0].update(result=[]),
                   lambda f: f.work('A').update(current_acceptance=[]),
                   lambda f: f.work('A')['acceptances'][0].update(result=[]),
                   lambda f: f.work('D')['assignment']['dependencies'][0].update(work=[]),
                   lambda f: f.work('D').update(active_attempt=[]),
                   lambda f: f.work('A')['attempts'][0]['result']['checks'][0]['covers'][0].update(criterion={})]
        for change in changes:
            self.f.data = copy.deepcopy(original)
            change(self.f)
            self.f.save()
            self.check(1)

    def test_common_preparation_cannot_grant_local_implementation(self):
        from test_recovery import FeatureFixture
        self.f = FeatureFixture(Path(self.tmp.name) / 'common-prepare')
        self.check()
        common = self.f.work('notes-feature')['assignment']
        common['stage'] = 'prepare'
        self.f.rebind_parent()
        self.check(1, 'общее prepare не разрешает местный implement')
        common['mandate']['stages'] = ['prepare']
        self.f.rebind_parent()
        self.check(1, 'этап не разрешён общим мандатом')
        local = self.f.local['works'][0]
        local['assignment']['stage'] = 'prepare'
        local['attempts'][0]['effects'] = ['read']
        self.f.save_local()
        self.check()  # Real preparation remains permitted.
        common['stage'] = 'implement'
        common['mandate']['stages'] = ['prepare', 'implement']
        self.f.rebind_parent()
        self.check()  # A general implementation grant can still delegate analysis.
        local['assignment']['stage'] = 'implement'
        local['attempts'][0]['effects'] = ['read', 'write', 'git']
        self.f.save_local()
        self.check()

    def test_historical_cwd_survives_actual_move_and_new_r1_confirmation(self):
        old = self.f.submit('A')
        command = [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', '.', '-v']
        run = subprocess.run(command, cwd=self.f.root / 'code', env=environment(), capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        old['checks'][0].update(command=' '.join(command), cwd='code', observed=run.stdout + run.stderr)
        self.f.accept('A', handoff=False)
        self.check()
        (self.f.root / 'code').rename(self.f.root / 'src')
        b = self.f.add_work('B')
        b['assignment'].update(inputs=[PROFILE, 'src'], write_scope=['src'])
        self.f.running('B')
        checked = self.f.commit('Move code to src while retaining historical A/cwd')
        command = [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'src', '-v']
        run = subprocess.run(command, cwd=self.f.root, env=environment(), capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        current = copy.deepcopy(old)
        current.update(id='B-result-1', assignment_id='B-assignment', attempt_id='B-attempt-1', executor='B-executor',
                       commit=checked, artifacts=['src/source.py', 'src/test_source.py'])
        current['checks'][0].update(id='B-check-1', command=' '.join(command), cwd='.', observed=run.stdout + run.stderr,
                                   covers=[{'work': x, 'assignment_revision': 1, 'criterion': x + '-criterion'} for x in ('A', 'B')])
        b['attempts'][0].update(phase='finished', termination={'by': 'test-parent', 'fact': 'Actual post-move check returned 0'}, result=current)
        b['status'] = 'submitted'
        self.f.data.pop('write_owner', None)
        self.f.data.pop('git_owner', None)
        self.f.action('B', 'review', [])
        self.f.save()
        self.check()  # Candidate B is not blocked by historical cwd of A.
        self.f.accept('B', handoff=False)
        dep = {'work': 'A', 'acceptance': 'A-acceptance-1', 'criterion': 'A-criterion'}
        d = self.f.add_work('D', dependencies=[dep])
        d['assignment'].update(inputs=[PROFILE, 'src'], write_scope=['src'])
        selected = {**dep, 'result': 'B-result-1', 'result_revision': 1, 'check': 'B-check-1',
                    'replacements': [{'result': old['id'], 'revision': 1, 'check': 'A-check-1',
                                      'reason': 'The new saved src check confirms the same storage obligation after the move', 'by': 'product-coordinator'}]}
        self.f.action('D', 'assign', [selected])
        self.f.save()
        self.assertFalse((self.f.root / 'code').exists())
        self.assertEqual(old['checks'][0]['cwd'], 'code')
        self.check()
        for unsafe in ('../outside', '/outside', 'never-in-the-checked-commit', 'code/source.py'):
            old['checks'][0]['cwd'] = unsafe
            self.f.save()
            self.check(1)
        old['checks'][0]['cwd'] = 'code'
        # The current B candidate still cannot claim a missing current cwd.
        current['checks'][0]['cwd'] = 'code'
        self.f.work('D')['status'] = 'paused'
        self.f.work('D')['blocker'] = {'reason': 'Control check', 'next_step': 'Inspect'}
        self.f.work('D')['attempts'][0].update(phase='stopped', termination={'by': 'test-parent', 'fact': 'Control assignment not started'})
        self.f.action('D', 'inspect')
        self.f.action('B', 'handoff', [])
        self.f.save()
        self.check(1)  # Its claimed cwd also did not exist in B's checked commit.

    def test_two_sequential_iterations_preserve_prior_integrated_history(self):
        self.f.work('A')['kind'] = 'integration'
        a = self.f.submit('A')
        self.f.accept('A', handoff=False)
        self.f.data['iterations'][0].update(status='integrated', integration='A-acceptance-1')
        self.f.data['iterations'].append({'id': 'iteration-2', 'goal': 'Add persisted notes', 'status': 'active'})
        dep = {'work': 'A', 'acceptance': 'A-acceptance-1', 'criterion': 'A-criterion'}
        b = self.f.add_work('B', dependencies=[dep])
        b.update(kind='integration', iteration='iteration-2')
        (self.f.root / 'code/source.py').write_text(BASE_CODE + NOTE_CODE)
        (self.f.root / 'code/test_source.py').write_text(BASE_TESTS + NOTE_TESTS)
        candidate = self.f.submit('B', [{'work': 'A', 'assignment_revision': 1, 'criterion': 'A-criterion'}])
        self.f.action('B', 'review', [{**dep, 'result': candidate['id'], 'result_revision': 1, 'check': 'B-check-1',
                                     'replacements': [{'result': a['id'], 'revision': 1, 'check': 'A-check-1',
                                                       'reason': 'Second iteration verifies the retained first-iteration obligation', 'by': 'product-coordinator'}]}])
        self.f.save()
        self.check()
        self.f.accept('B', handoff=False)
        self.f.data['iterations'][1].update(status='integrated', integration='B-acceptance-1')
        self.f.save()
        self.check()
        self.assertNotEqual(a['commit'], candidate['commit'])


if __name__ == '__main__':
    unittest.main()
