// OpenCode server plugin: virtual-fingers.
// Registers a list tool and an actuation tool per vendor driver. Tool names,
// descriptions, actions and parameters all come from the Python driver registry
// (`vf.py schema`), so adding a driver in vf/drivers/ adds its tools here.
import { Plugin } from "@opencode/plugin"
import { execFileSync, spawn } from "node:child_process"
import path from "node:path"
import { fileURLToPath } from "node:url"

const DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const PY = process.env.VIRTUAL_FINGERS_PYTHON ?? path.join(DIR, ".venv", "bin", "python")

type Param = { name: string; description: string; default: number; min: number; max: number }
type ActionSpec = { name: string; description: string; actuates: boolean; params: Param[] }
type DriverSpec = {
  vendor: string
  label: string
  types: string[]
  list_tool: {
    name: string
    description: string
    heading: string
    default_duration: number
    hidden_param: { name: string; description: string } | null
  }
  act_tool: { name: string; description: string; default_action: string }
  actions: ActionSpec[]
}

function loadDrivers(): DriverSpec[] {
  try {
    const out = execFileSync(PY, ["vf.py", "schema"], { cwd: DIR, encoding: "utf8", timeout: 30_000 })
    return (JSON.parse(out) as { drivers: DriverSpec[] }).drivers
  } catch (e) {
    throw new Error(
      `virtual-fingers: could not load driver registry with ${PY} (run \`uv sync\` in ${DIR}): ${
        e instanceof Error ? e.message : String(e)
      }`,
    )
  }
}

function run(args: string[], signal?: AbortSignal): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn(PY, ["vf.py", ...args], { cwd: DIR })
    let out = ""
    let err = ""
    const kill = () => child.kill("SIGKILL")
    child.stdout.on("data", (d) => (out += d))
    child.stderr.on("data", (d) => (err += d))
    signal?.addEventListener("abort", kill, { once: true })
    child.on("error", (e) => {
      signal?.removeEventListener("abort", kill)
      reject(new Error(`failed to start ${PY}: ${e.message}`))
    })
    child.on("close", (code) => {
      signal?.removeEventListener("abort", kill)
      const text = `${out.trim()}${err.trim() ? `\n${err.trim()}` : ""}`
      if (code === 0) resolve(text)
      else reject(new Error(`vf.py exited with code ${code}: ${text || "no output"}`))
    })
  })
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

const clamp = (p: Param, raw: unknown) =>
  Math.min(Math.max(Math.round(raw === undefined ? p.default : Number(raw) || p.default), p.min), p.max)

export default Plugin.define({
  id: "virtual-fingers",
  async setup(ctx) {
    const drivers = loadDrivers()

    await ctx.tool.transform((editor) => {
      for (const driver of drivers) {
        const lt = driver.list_tool
        const listProps: Record<string, unknown> = {
          duration: {
            type: "number",
            description: `scan duration in seconds (default ${lt.default_duration}, max 60)`,
          },
        }
        if (lt.hidden_param) listProps[lt.hidden_param.name] = { type: "boolean", description: lt.hidden_param.description }
        if (driver.types.length) {
          listProps.types = {
            type: "array",
            items: { type: "string", enum: driver.types },
            description: "only list devices of these types (default: all)",
          }
        }

        editor.add({
          name: lt.name,
          description: lt.description,
          input: { type: "object", properties: listProps, additionalProperties: false },
          options: { codemode: true },
          execute: async (input, context) => {
            const i = input as Record<string, unknown>
            const duration = Math.min(Math.max(Math.round(Number(i.duration) || lt.default_duration), 3), 60)
            const types = (i.types as string[] | undefined) ?? []
            const invalid = types.filter((t) => !driver.types.includes(t))
            if (invalid.length) {
              return { content: `Invalid type(s) ${invalid.join(", ")}. Use: ${driver.types.join(", ")}.` }
            }
            await context.progress({ status: `Scanning BLE for ${driver.label} devices (${duration}s)...` })
            const args = ["list", "--json", "--vendor", driver.vendor, "--duration", String(duration)]
            if (lt.hidden_param && i[lt.hidden_param.name] === true) args.push("--all")
            for (const t of types) args.push("--type", t)
            try {
              return { content: `${lt.heading}:\n${await run(args, context.signal)}` }
            } catch (e) {
              return { content: `${lt.name} FAILED: ${message(e)}` }
            }
          },
        })

        const at = driver.act_tool
        const actionNames = driver.actions.map((a) => a.name)
        const params = new Map<string, Param>()
        for (const a of driver.actions) for (const p of a.params) params.set(p.name, p)
        const actProps: Record<string, unknown> = {
          mac: {
            type: "string",
            description: `BT MAC address from ${lt.name}, e.g. AA:BB:CC:DD:EE:FF`,
          },
          action: {
            type: "string",
            enum: actionNames,
            description: `what to do (default: ${at.default_action})`,
          },
        }
        for (const p of params.values()) actProps[p.name] = { type: "number", description: p.description }

        editor.add({
          name: at.name,
          description: at.description,
          input: { type: "object", properties: actProps, required: ["mac"], additionalProperties: false },
          options: { codemode: true },
          execute: async (input, context) => {
            const i = input as Record<string, unknown>
            const mac = String(i.mac)
            const name = String(i.action ?? at.default_action)
            const action = driver.actions.find((a) => a.name === name)
            if (!action) {
              return { content: `Invalid action "${name}". Use one of: ${actionNames.join(", ")}.` }
            }
            if (action.actuates) {
              // Physical actuation: ask the first time per device; "always allow" persists.
              await ctx.permission.assert({
                sessionID: context.sessionID,
                agent: context.agent,
                source: { type: "tool", messageID: context.messageID, id: context.id },
                action: "actuate",
                resources: [mac],
                save: [mac],
              })
            }
            const args = [action.name, mac, "--vendor", driver.vendor]
            const detail: string[] = []
            for (const p of action.params) {
              const v = clamp(p, i[p.name])
              args.push(`--${p.name}`, String(v))
              detail.push(`${p.name}=${v}`)
            }
            await context.progress({
              status: `${driver.label} BLE: ${action.name}${detail.length ? ` (${detail.join(", ")})` : ""} ${mac}`,
            })
            try {
              return { content: `${driver.label} ${action.name} ${mac}: OK\n${await run(args, context.signal)}` }
            } catch (e) {
              return { content: `${driver.label} ${action.name} ${mac} FAILED: ${message(e)}` }
            }
          },
        })
      }
    })
  },
})
