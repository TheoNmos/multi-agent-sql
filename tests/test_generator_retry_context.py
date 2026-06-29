"""Tests for generator retry context rendering (no duplicate user-message feedback)."""

from __future__ import annotations

import unittest

from pydantic_ai import RunContext

from app.agents.context import AgentState, ValidatorOutput
from app.agents.generator import _build_generator_template_vars


class GeneratorRetryContextTests(unittest.TestCase):
    def test_iteration_context_includes_validator_and_execution_feedback(self) -> None:
        state = AgentState(
            raw_question="count users",
            clarified_question="count users",
            attempt_count=1,
            current_sql="SELECT 1",
            validator_output=ValidatorOutput(
                is_valid=False,
                is_optimal=False,
                refinement_feedback="missing WHERE country = 'SVK'",
            ),
        )
        state.scratch["execution_feedback"] = "Execution error: column foo does not exist"
        ctx = RunContext(deps=state, model=None, usage=None, prompt=None, messages=[])
        vars_ = _build_generator_template_vars(ctx)
        iteration = vars_["iteration_context"]
        self.assertIn("missing WHERE country", iteration)
        self.assertIn("column foo does not exist", iteration)

    def test_first_attempt_has_no_iteration_context(self) -> None:
        state = AgentState(raw_question="count users", clarified_question="count users")
        ctx = RunContext(deps=state, model=None, usage=None, prompt=None, messages=[])
        vars_ = _build_generator_template_vars(ctx)
        self.assertEqual(vars_["iteration_context"], "")


if __name__ == "__main__":
    unittest.main()
