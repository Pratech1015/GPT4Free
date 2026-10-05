// CLI wrapper: reads {"p": ..., "dx": ...} on stdin, prints {"token": ...} on stdout.
// Used by gptpp.solver.solve_turnstile() via the system Node.js runtime.
import { solveTurnstile } from "./turnstile.mjs";

let input = "";
for await (const chunk of process.stdin) {
  input += chunk;
}

try {
  const { p, dx } = JSON.parse(input);
  const result = await solveTurnstile(p, dx, 5000);
  process.stdout.write(JSON.stringify(result));
} catch (err) {
  process.stdout.write(
    JSON.stringify({ error: String((err && err.message) || err) })
  );
  process.exitCode = 1;
}
