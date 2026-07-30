import pytest

from growth_engine.models import WorkflowState, require_transition
from growth_engine.policy import Action, PolicyViolation, authorize


@pytest.mark.parametrize(
    "action",
    [
        Action.ENGAGE,
        Action.CREATE_AUDIENCE_ACCOUNT,
        Action.ARTIFICIAL_TRAFFIC,
        Action.BYPASS_PROTECTION,
        Action.MANIPULATE_METRICS,
        Action.PUBLISH,
    ],
)
def test_disallowed_actions_are_rejected(action: Action) -> None:
    with pytest.raises(PolicyViolation):
        authorize(action)


def test_platform_read_requires_official_api() -> None:
    with pytest.raises(PolicyViolation, match="official API"):
        authorize(Action.OFFICIAL_API_READ)
    authorize(Action.OFFICIAL_API_READ, official_api=True)


def test_workflow_state_transitions_are_explicitly_enforced() -> None:
    require_transition(WorkflowState.COLLECTED, WorkflowState.GENERATED)
    with pytest.raises(ValueError, match="collected -> ranked"):
        require_transition(WorkflowState.COLLECTED, WorkflowState.RANKED)
