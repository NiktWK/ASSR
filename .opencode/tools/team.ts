import { tool } from "@opencode-ai/plugin"

type Args = {
  action: "submit" | "review" | "assign" | "ask" | "answer" | "sources" | "status"
  phase?: "plan" | "build"
  content?: string
  task_id?: string
  verdict?: "approve" | "revise"
  comments?: string
  worker?: "developer" | "tester"
  goal?: string
  context?: string
  question?: string
  question_id?: string
  answer?: string
  url?: string
  note?: string
}

export default tool({
  description:
    "Team protocol tool. All communication with other agents goes through it. " +
    "submit (phase, content[, task_id]) - hand in the PLAN or BUILD result and end your turn; " +
    "review (phase, verdict approve|revise, comments[, task_id]) - verdict on a submitted plan/report " +
    "(researcher reviews developer/tester with task_id; controller reviews researcher without task_id); " +
    "assign (worker developer|tester, goal[, context]) - researcher in BUILD creates a task; " +
    "ask (question) - blocking question to your supervisor, ends your turn, the answer arrives as a new message; " +
    "answer (question_id, answer) - supervisor replies to a subordinate's question; " +
    "sources (url[, note]) - researcher records an internet source; " +
    "status - compact state overview.",
  args: {
    action: tool.schema
      .enum(["submit", "review", "assign", "ask", "answer", "sources", "status"])
      .describe("Directive to execute"),
    phase: tool.schema.enum(["plan", "build"]).optional().describe("Phase for submit/review"),
    content: tool.schema.string().optional().describe("Full plan or report text for submit"),
    task_id: tool.schema.string().optional().describe("Task id for submit/review of developer/tester work"),
    verdict: tool.schema.enum(["approve", "revise"]).optional().describe("Review verdict"),
    comments: tool.schema.string().optional().describe("Concrete review comments, required for revise"),
    worker: tool.schema.enum(["developer", "tester"]).optional().describe("assign: who gets the task"),
    goal: tool.schema.string().optional().describe("assign: concrete goal for the worker"),
    context: tool.schema.string().optional().describe("assign: architecture/context for the worker"),
    question: tool.schema.string().optional().describe("ask: the question text"),
    question_id: tool.schema.string().optional().describe("answer: question id, e.g. q3"),
    answer: tool.schema.string().optional().describe("answer: answer text"),
    url: tool.schema.string().optional().describe("sources: source URL"),
    note: tool.schema.string().optional().describe("sources: short note about the source"),
  },
  async execute(args, context) {
    const session = String((context as { sessionID?: string }).sessionID ?? "")
    const agent = String((context as { agent?: string }).agent ?? "")
    const cmd = ["python3", "team_agent.py", "--session", session, "--agent", agent, args.action]
    const pairs: [string, keyof Args][] = [
      ["--phase", "phase"],
      ["--content", "content"],
      ["--task-id", "task_id"],
      ["--verdict", "verdict"],
      ["--comments", "comments"],
      ["--worker", "worker"],
      ["--goal", "goal"],
      ["--context", "context"],
      ["--question", "question"],
      ["--question-id", "question_id"],
      ["--answer", "answer"],
      ["--url", "url"],
      ["--note", "note"],
    ]
    for (const [flag, key] of pairs) {
      const value = args[key]
      if (value !== undefined) cmd.push(flag, String(value))
    }
    const proc = Bun.spawnSync({ cmd, stdout: "pipe", stderr: "pipe" })
    const out = proc.stdout.toString().trim()
    const err = proc.stderr.toString().trim()
    if (proc.exitCode !== 0) {
      throw new Error(err || out || `team_agent.py exited with code ${proc.exitCode}`)
    }
    return out || "ok"
  },
})