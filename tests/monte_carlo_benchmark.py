"""Reproducible full-size benchmark; uses only disposable synthetic data."""
import sys
import argparse
import cProfile
import pstats
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from decimal import Decimal as D

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from app.extensions import db
from retirement_demo import seed_demo
from test_retirement_affordability import inputs
from app.models import Portfolio
from sqlalchemy import select
from app.services.retirement_affordability import prepare_affordability, solve_affordability
from app.services.retirement_monte_carlo import PATH_COUNT, prepare_experiment, compare_allocations
from app.services.retirement import _distribute

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--paths', type=int, default=PATH_COUNT)
    parser.add_argument('--source-copies', type=int, default=1)
    parser.add_argument('--profile', action='store_true')
    args = parser.parse_args()
    if args.source_copies < 1 or args.paths < 1:
        parser.error('paths and source-copies must be positive')
    with TemporaryDirectory(prefix='lt-monte-carlo-benchmark-') as directory:
        app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{directory}/test.sqlite3'})
        with app.app_context():
            db.create_all()
            seed_demo('funded')
            portfolio = db.session.scalar(select(Portfolio))
            basis, summary = prepare_affordability(portfolio, inputs(
                current_age_years=50, withdrawal_start_age_years=52, final_age_years=93,
                core_amount=D('42000'), inflation_decimal=D('.025'),
                equity_return_decimal=D('.05'), income_return_decimal=D('.02'),
                liquidity_return_decimal=D('.01'), alternatives_return_decimal=D('.02')))
            experiment = prepare_experiment(basis, summary, solve_affordability(basis)['flexible_amount'])
            pools = experiment['basis']['_inputs']['pools']
            expanded = []
            for pool in pools:
                amounts = {role: _distribute(value, dict.fromkeys(range(args.source_copies), D(1))) for role, value in pool['roles'].items()}
                for index in range(args.source_copies):
                    expanded.append({**pool, 'source_key': f"{pool['source_key']}:copy{index}",
                                     'roles': {role: values[index] for role, values in amounts.items()}})
            experiment['basis']['_inputs']['pools'] = expanded
            profile = cProfile.Profile() if args.profile else None
            start = perf_counter()
            if profile:
                profile.enable()
            result = compare_allocations(experiment, dict(zip(('equity','income','liquidity','alternatives'), map(D, ('50','30','10','10')))), path_count=args.paths)
            if profile:
                profile.disable()
            print({'paths_per_allocation': result['path_count'], 'years': 43,
                   'starting_sources': len(expanded),
                   'seconds': round(perf_counter() - start, 3),
                   'outcomes': [row['outcomes'] for row in result['allocations']]})
            if profile:
                pstats.Stats(profile).sort_stats('cumtime').print_stats(15)
