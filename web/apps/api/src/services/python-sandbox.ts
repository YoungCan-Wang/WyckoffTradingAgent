import type { Env } from '../app'

const DEFAULT_TIMEOUT_MS = 60_000
const MAX_TIMEOUT_MS = 120_000
const MAX_OUTPUT_BYTES = 32 * 1024
const RUN_PYTHON = 'ulimit -f 64; python3 main.py >stdout.txt 2>stderr.txt; code=$?; cat stdout.txt; cat stderr.txt >&2; exit $code'

type CommandResult = {
  exitCode: number
  stdout: () => Promise<string>
  stderr: () => Promise<string>
}

type SandboxUsage = {
  activeCpuUsageMs: number
  networkTransfer: { ingress: number; egress: number }
}

export type SandboxHandle = {
  writeFiles: (files: Array<{ path: string; content: string }>) => Promise<void>
  runCommand: (command: string, args: string[]) => Promise<CommandResult>
  stop: () => Promise<SandboxUsage>
  delete: () => Promise<void>
}

export type PythonSandboxOptions = {
  runtime: 'python3.13'
  timeout: number
  networkPolicy: 'deny-all'
  persistent: false
  resources: { vcpus: 1 }
  tags: { app: 'wyckoff'; kind: 'python-research' }
}

export type SandboxFactory = (env: Env, options: PythonSandboxOptions) => Promise<SandboxHandle>

export type PythonSandboxResult = {
  exitCode: number
  stdout: string
  stderr: string
  activeCpuUsageMs: number
  networkIngressBytes: number
  networkEgressBytes: number
}

export async function executePythonSandbox(
  env: Env,
  script: string,
  createSandbox: SandboxFactory = createVercelSandbox,
): Promise<PythonSandboxResult> {
  const sandbox = await createSandbox(env, sandboxOptions(env))
  try {
    await sandbox.writeFiles([{ path: 'main.py', content: script }])
    const command = await sandbox.runCommand('sh', ['-c', RUN_PYTHON])
    const [stdout, stderr] = await Promise.all([command.stdout(), command.stderr()])
    const usage = await sandbox.stop()
    return {
      exitCode: command.exitCode,
      stdout: limitOutput(stdout),
      stderr: limitOutput(stderr),
      activeCpuUsageMs: usage.activeCpuUsageMs ?? 0,
      networkIngressBytes: usage.networkTransfer?.ingress ?? 0,
      networkEgressBytes: usage.networkTransfer?.egress ?? 0,
    }
  } finally {
    await sandbox.delete().catch(() => undefined)
  }
}

async function createVercelSandbox(env: Env, options: PythonSandboxOptions): Promise<SandboxHandle> {
  const credentials = sandboxCredentials(env)
  const { Sandbox } = await import('@vercel/sandbox')
  return Sandbox.create({ ...credentials, ...options })
}

function sandboxOptions(env: Env): PythonSandboxOptions {
  return {
    runtime: 'python3.13',
    timeout: sandboxTimeout(env.AGENT_SANDBOX_TIMEOUT_MS),
    networkPolicy: 'deny-all',
    persistent: false,
    resources: { vcpus: 1 },
    tags: { app: 'wyckoff', kind: 'python-research' },
  }
}

function sandboxCredentials(env: Env) {
  const token = env.VERCEL_OIDC_TOKEN?.trim() || env.VERCEL_TOKEN?.trim()
  const teamId = env.VERCEL_TEAM_ID?.trim()
  const projectId = env.VERCEL_PROJECT_ID?.trim()
  if (!token || !teamId || !projectId) throw new Error('Vercel Sandbox env is incomplete')
  return { token, teamId, projectId }
}

function sandboxTimeout(raw: string | undefined): number {
  const parsed = Number(raw)
  if (!Number.isFinite(parsed) || parsed < 5_000) return DEFAULT_TIMEOUT_MS
  return Math.min(Math.trunc(parsed), MAX_TIMEOUT_MS)
}

function limitOutput(value: string): string {
  const encoded = new TextEncoder().encode(value)
  if (encoded.length <= MAX_OUTPUT_BYTES) return value
  return `${new TextDecoder().decode(encoded.slice(0, MAX_OUTPUT_BYTES))}\n...[truncated]`
}
