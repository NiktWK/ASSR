import { tool } from "@opencode-ai/plugin"

type Args = {
  action:
    | "status"
    | "tasks"
    | "events"
    | "questions"
    | "question"
    | "answer"
    | "start"
    | "stop"
    | "tell_controller"
    | "result"
  limit?: number
  id?: string
  text?: string
  task?: string
}

export default tool({
  description:
    "Secretary tool for the operator's personal assistant. Directives: " +
    "status (run status, phases, tasks, open questions), tasks, events (metadata-only history), " +
    "questions (open human questions), question (id, full text), " +
    "answer (id, text - answer on the operator's behalf), " +
    "start (task - start a new run; refused while another run is active), " +
    "stop (cancel the active run), " +
    "tell_controller (text - deliver the operator's instruction to the controller of the active run), " +
    "result (latest controller final conclusion).",
  args: {
    action: tool.schema
      .enum([
        "status",
        "tasks",
        "events",
        "questions",
        "question",
        "answer",
        "start",
        "stop",
        "tell_controller",
        "result",
      ])
      .describe("Directive to execute"),
    limit: tool.schema.number().optional().describe("events: how many recent events (default 20, max 100)"),
    id: tool.schema.string().optional().describe("question/answer: question id, e.g. q3"),
    text: tool.schema.string().optional().describe("answer text or tell_controller instruction"),
    task: tool.schema.string().optional().describe("start: task goal for the team"),
  },
  async execute(args, context) {
    const session = String((context as { sessionID?: string }).sessionID ?? "")
    const cmd = ["python3", "assistant_agent.py", "--session", session, args.action]
    if (args.limit !== undefined) cmd.push("--limit", String(Math.round(args.limit)))
    if (args.id !== undefined) cmd.push("--id", String(args.id))
    if (args.text !== undefined) cmd.push("--text", String(args.text))
    if (args.task !== undefined) cmd.push("--task", String(args.task))
    const proc = Bun.spawnSync({ cmd, stdout: "pipe", stderr: "pipe" })
    const out = proc.stdout.toString().trim()
    const err = proc.stderr.toString().trim()
    if (proc.exitCode !== 0) {
      throw new Error(err || out || `assistant_agent.py exited with code ${proc.exitCode}`)
    }
    return out || "ok"
  },
})