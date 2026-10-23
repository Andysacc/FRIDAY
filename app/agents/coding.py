from app.execution.service import ExecutionService
from app.orchestrator.contracts import AgentResultMessage, AgentTaskMessage


class CodingAgent:
    """Thin specialist boundary for code validation tasks.

    The agent never starts a process itself. It can only send a validated JSON
    execution request to the deterministic execution service.
    """

    name = "coding"

    def __init__(self, execution_service: ExecutionService) -> None:
        self._execution_service = execution_service

    async def run(self, task: AgentTaskMessage) -> AgentResultMessage:
        result = await self._execution_service.execute(task.execution)
        succeeded = result.status == "succeeded"
        findings = [
            f"{result.tool}.{result.action} {result.status}",
            f"exit_code={result.exit_code}" if result.exit_code is not None else "no exit code",
        ]
        if result.error:
            findings.append(result.error)

        return AgentResultMessage(
            task_id=task.task_id,
            status="succeeded" if succeeded else "failed",
            execution=result,
            findings=findings,
        )
