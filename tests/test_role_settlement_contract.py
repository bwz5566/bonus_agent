"""Exercise a full project-to-recipient funding path under changing role rates."""

from decimal import Decimal

import pytest

from bonus_core.data.table import from_records
from bonus_core.dsl.models import RuleSet
from bonus_core.engine.issues import EngineError
from bonus_core.engine.run import Engine, compute


@pytest.mark.parametrize(
    ('rate', 'sample_contribution', 'recipient_total', 'pending', 'residual'),
    [('0.25', '125', '125.01', '750.03', '-0.01'),
     ('0.40', '200', '200.01', '600.02', '0.00')],
)
@pytest.mark.parametrize('entity_funding', [False, True])
def test_role_and_personal_shares_are_multiplied_before_rounding_by_recipient(
    rate, sample_contribution, recipient_total, pending, residual, entity_funding
):
    sources = [{'doc': 'policy.md', 'quote': 'Apply role rates, then personal shares. Round per recipient.'}]
    datasets = {
        'projects': {'key': 'pid', 'fields': {'pid': {'type': 'id'}, 'amount': {'type': 'number'},
                                             'unpaid': {'type': 'number'}}},
        'people': {'key': 'person', 'fields': {'person': {'type': 'id'}}},
        'contributions': {'key': 'rid', 'link': 'person', 'fields': {
            'rid': {'type': 'id'}, 'pid': {'type': 'id'}, 'person': {'type': 'id'},
            'role': {'type': 'text'}, 'personal_share': {'type': 'number'},
        }},
    }
    project_amounts = [('P', '1000', 0), ('Q', '0.02', 0), ('R', '0.02', 0), ('H', '200', 1)]
    records = {
        'projects': [{'pid': pid, 'amount': amount, 'unpaid': unpaid} for pid, amount, unpaid in project_amounts],
        'people': [{'person': name} for name in ('A', 'B')],
        'contributions': [
            {'rid': pid + name, 'pid': pid, 'person': name, 'role': 'sales', 'personal_share': '0.5'}
            for pid, _, _ in project_amounts for name in ('A', 'B')
        ],
    }
    funding_definition = {
        'name': 'Project', 'version': '1', 'datasets': datasets,
        'allocation': [{'level': 'projects', 'key': 'pid'}],
        'pool': {'scope': 'projects', 'expr': 'round(if(unpaid == 1, 0, amount), 2)'},
    }
    if entity_funding:
        funding_definition['allocation'] = []
        funding_definition['report'] = {
            'totals': [{'name': 'funds', 'level': 'projects', 'expr': 'round(if(unpaid == 1, 0, amount), 2)'}],
            'details': [{'name': 'funds', 'level': 'projects', 'expr': 'round(if(unpaid == 1, 0, amount), 2)',
                         'sources': sources}],
        }
    project_rules = RuleSet.model_validate(funding_definition)
    project_result = compute(project_rules, from_records(project_rules, records))
    if entity_funding:
        basis = Engine.prepare_basis(project_rules, from_records(project_rules, records)).calculate_basis()
        assert project_result.funding == {key: row['amount'] for key, row in basis['objects'].items()}
        assert project_result.pool == basis['pool'] and not project_result.people and not project_result.payouts

    person_rules = RuleSet.model_validate({
        'name': 'Recipient', 'version': '1', 'datasets': datasets,
        'allocation': [{'level': 'people', 'key': 'person'}],
        'tables': {'role_rates': {'kind': 'lookup', 'mapping': {'sales': rate, 'NPD': str(1 - Decimal(rate))}}},
        'variables': {
            'funding': {'scope': 'projects', 'expr': 'scheme_paid("Project")'},
            'contribution': {'scope': 'contributions', 'expr': 'funding * lookup(role_rates, role) * personal_share'},
            'recipient_raw': {'scope': 'people', 'expr': 'sum(contribution, of: "contributions")'},
        },
        'pool': {'scope': 'people', 'expr': 'recipient_raw'},
        'report': {
            'people': 'person',
            'details': [{'name': 'role contribution', 'level': 'contributions', 'expr': 'contribution', 'sources': sources}],
            'pending': [{'name': 'NPD pending', 'level': 'projects',
                         'expr': 'funding * lookup(role_rates, "NPD")', 'sources': sources}],
            'distributions': [{'name': 'funding', 'level': 'global', 'total': 'sum(funding, of: "projects")',
                               'pending': ['NPD pending'], 'rounding': 'after_sum', 'sources': sources}],
        },
    })
    if entity_funding:
        from bonus_core.dsl.models import FundingReconciliation

        person_rules.report.distributions[0].source_funding = FundingReconciliation(
            scheme='Project', level='projects', allocated='sum(contribution, of: "contributions")')
    people_data = from_records(person_rules, records)
    reference = {'Project/R0': project_result}
    result = compute(person_rules, people_data, refs=reference)

    contributions = {row['key']: Decimal(row['amount']) for row in result.details['role contribution'].values()}
    assert contributions['PA'] == Decimal(sample_contribution)
    assert contributions['HA'] == 0
    assert contributions['QA'] == 0
    raw = result.detail_raw_amounts()['role contribution']
    assert raw['contributions:QA'] == Decimal('0.02') * Decimal(rate) * Decimal('0.5')
    people = {row['name']: Decimal(row['paid']) for row in result.people}
    assert people == {'A': Decimal(recipient_total), 'B': Decimal(recipient_total), 'NPD pending': Decimal('0')}
    pending_row = next(row for row in result.people if row['name'] == 'NPD pending')
    assert pending_row['pending'] and Decimal(pending_row['final']) == Decimal(pending)
    assert Decimal(result.rounding[0]['residual']) == Decimal(residual)
    if entity_funding:
        contract = person_rules.report.distributions[0].source_funding
        assert result.rounding[0]['funding_objects'] == 4
        contract.allocated += ' + if(pid == "P", 1, if(pid == "Q", -1, 0))'
        with pytest.raises(EngineError, match='资金.*与实名原值'):
            compute(person_rules, people_data, refs=reference)
