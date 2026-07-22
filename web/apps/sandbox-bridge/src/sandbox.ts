import { Sandbox } from '@vercel/sandbox'

const MAX_OUTPUT_BYTES = 32 * 1024
const RUN_PYTHON = 'ulimit -f 64; python3 main.py >stdout.txt 2>stderr.txt; code=$?; cat stdout.txt; cat stderr.txt >&2; exit $code'

export type PythonSandboxResult = {
  exitCode: number
  stdout: string
  stderr: string
  activeCpuUsageMs: number
  networkIngressBytes: number
  networkEgressBytes: number
}

export async function executePythonSandbox(script: string, timeout: number): Promise<PythonSandboxResult> {
  const sandbox = await Sandbox.create({
    ...localCredentials(),
    runtime: 'python3.13',
    timeout,
    networkPolicy: 'deny-all',
    persistent: false,
    resources: { vcpus: 1 },
    tags: { app: 'wyckoff', kind: 'python-research' },
  })
  try {
    await sandbox.writeFiles([{ path: 'main.py', content: Buffer.from(script) }])
    const command = await sandbox.runCommand('sh', ['-c', RUN_PYTHON], { timeoutMs: timeout })
    const [stdout, stderr] = await Promise.all([command.stdout(), command.stderr()])
    await sandbox.stop()
    return usageResult(command.exitCode, stdout, stderr, sandbox)
  } finally {
    await sandbox.delete().catch(() => undefined)
  }
}

function localCredentials() {
  const token = process.env.VERCEL_TOKEN?.trim()
  const teamId = process.env.VERCEL_TEAM_ID?.trim()
  const projectId = process.env.VERCEL_PROJECT_ID?.trim()
  return token && teamId && projectId ? { token, teamId, projectId } : {}
}

function usageResult(
  exitCode: number,
  stdout: string,
  stderr: string,
  sandbox: Sandbox,
): PythonSandboxResult {
  return {
    exitCode,
    stdout: limitOutput(stdout),
    stderr: limitOutput(stderr),
    activeCpuUsageMs: sandbox.activeCpuUsageMs ?? 0,
    networkIngressBytes: sandbox.networkTransfer?.ingress ?? 0,
    networkEgressBytes: sandbox.networkTransfer?.egress ?? 0,
  }
}

function limitOutput(value: string): string {
  const output = Buffer.from(value)
  if (output.length <= MAX_OUTPUT_BYTES) return value
  return `${output.subarray(0, MAX_OUTPUT_BYTES).toString()}\n...[truncated]`
}
