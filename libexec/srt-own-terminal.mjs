// Loaded into srt (node --import) on macOS. srt's allowPty setting allows
// terminal control on every /dev/ttys*: Claude's interface needs it on its own
// terminal (raw mode, to read keys), but on the others it would let Claude
// read what is typed there. This narrows the rule, in the Seatbelt profile srt
// passes to sandbox-exec, to the terminal in CLAUDE_SECURE_SANDBOX_TTY. When
// the rule is not found, nothing runs.
import { createRequire, syncBuiltinESMExports } from 'node:module';

const childProcess = createRequire(import.meta.url)('node:child_process');
const RULE = '(regex #"^/dev/ttys")';
const tty = process.env.CLAUDE_SECURE_SANDBOX_TTY || '';
delete process.env.CLAUDE_SECURE_SANDBOX_TTY;

function refuse(reason) {
  process.stderr.write(`claude-secure-sandbox: ${reason}; nothing was run\n`);
  process.exit(1);
}

const spawn = childProcess.spawn;
childProcess.spawn = function (command, ...rest) {
  if (typeof command === 'string' && command.includes('/usr/bin/sandbox-exec')) {
    if (!/^\/dev\/ttys[0-9]+$/.test(tty)) refuse(`unexpected terminal "${tty}"`);
    if (!command.includes(RULE)) refuse("srt's terminal rule was not found (srt changed?)");
    command = command.split(RULE).join(`(literal "${tty}")`);
    if (command.includes('/dev/ttys")')) refuse("srt's terminal rule could not be narrowed");
  }
  return spawn.call(this, command, ...rest);
};
syncBuiltinESMExports();
