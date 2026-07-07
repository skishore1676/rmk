"""The brain: route a rendered note through Suman's agent-broker.

rmk does not call an LLM provider directly. It hands the note (as page-image
files) to the agent broker, which owns provider selection, failover, and
receipts. The broker's ``claude`` provider views the images via its Read tool
(see agent_broker/providers/claude.py) and returns the answer.

We use ``AgentTask.raw_prompt`` — the broker's "hire for my prompt" passthrough
— so the model's raw text comes back with no OUTCOME framing.
"""

from __future__ import annotations

from agent_broker import AgentBroker, AgentContext, AgentSpec, AgentTask
from agent_broker.policy import AgentPolicy, ProviderBinding, load_policy

# Cap how many page-images we hand to the model in one turn. A single brainstorm
# is rarely longer; we warn rather than silently truncate (see cli.py).
MAX_PAGES = 20

SUMMARY_PROMPT = (
    "The attached images are pages of my handwritten notes from a reMarkable "
    "tablet, in order. Read the handwriting and write a clear, well-structured "
    "summary: start with a one-line gist, then the key points as bullets, then "
    "any open questions or action items you can infer. Preserve my terminology."
)

DIAGRAM_PROMPT = (
    "The attached images are pages of my handwritten notes from a reMarkable "
    "tablet, in order. Read the handwriting and turn the ideas into a flow "
    "diagram using Mermaid syntax. Respond with ONLY a single ```mermaid code "
    "block (a flowchart, e.g. `flowchart TD`), no prose before or after. Use my "
    "own labels from the notes for the nodes."
)


class LLMError(RuntimeError):
    pass


def ask(
    image_paths: list[str],
    prompt: str,
    *,
    working_dir: str,
    provider: str = "claude",
    model: str = "sonnet",
    timeout: int = 600,
    policy_path: str | None = None,
    actor: str = "reader",
    role: str = "note_reader",
    lane: str = "rmk",
) -> str:
    """Route page images + a prompt through the broker; return the text answer.

    If ``policy_path`` is set, the broker resolves the provider chain from that
    policy (failover, receipts — the full "brain"). Otherwise it runs a single
    explicit ``provider``/``model`` binding, which still executes through the
    broker.
    """
    if not image_paths:
        raise LLMError("No page images to send.")

    spec = AgentSpec(lane_id=lane, actor=actor, role=role)
    task = AgentTask(
        task_id="rmk-note",
        objective="Read the attached handwritten note and answer the request.",
        raw_prompt=prompt,
        context=AgentContext(images=tuple(image_paths)),
        working_dir=working_dir,
        timeout_seconds=timeout,
    )

    if policy_path:
        try:
            policy: AgentPolicy = load_policy(policy_path)
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"Could not load broker policy {policy_path!r}: {e}") from e
        result = AgentBroker(policy).run(spec, task)
    else:
        binding = ProviderBinding(provider, {"model": model, "timeout_seconds": timeout})
        result = AgentBroker().run(spec, task, binding=binding)

    if result.status != "succeeded":
        summary = result.receipt.failure_summary or "provider chain failed"
        raise LLMError(
            f"The broker could not complete the request: {summary}\n"
            "Is the `claude` CLI logged in? Check with `claude auth status`."
        )
    return (result.output_text or "").strip()
