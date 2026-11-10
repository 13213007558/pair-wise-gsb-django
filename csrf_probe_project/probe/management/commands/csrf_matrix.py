from django.core.management.base import BaseCommand

from probe.matrix import run_matrix
from probe.report import fixed_credentials_banner, render_trace


class Command(BaseCommand):
    help = "Print the CSRF reverse-proxy decision-path matrix."

    def handle(self, *args, **options):
        self.stdout.write(fixed_credentials_banner())
        self.stdout.write("=" * 78)
        results = run_matrix()
        mismatches = 0
        for scenario, trace in results:
            self.stdout.write("")
            self.stdout.write(scenario.name)
            self.stdout.write("-" * 78)
            self.stdout.write(render_trace(trace))
            expectation = "accept" if scenario.expect_accept else "deny"
            observed = "accept" if trace.accepted else "deny"
            ok = trace.accepted == scenario.expect_accept
            mismatches += 0 if ok else 1
            self.stdout.write(
                f"  expected={expectation:<6} observed={observed:<6} "
                f"match={'OK' if ok else 'MISMATCH'}"
            )
            self.stdout.write(f"  remediation layer: {scenario.fixed_by}")
        self.stdout.write("=" * 78)
        self.stdout.write(
            f"{len(results)} scenarios, {mismatches} expectation mismatch(es)."
        )
