/**
 * Long-running proton-drive CLI for protondrivefs.
 *
 * Built from the CLI's own source (the PKGBUILD copies this file into
 * `cli/src`), so every command behaves and prints exactly like
 * `proton-drive <group> <command> ...`. Unlike the CLI, it starts the SDK
 * session once and keeps it, instead of paying for Bun startup, session
 * restore and key decryption on every folder listing.
 *
 * Protocol: one JSON object per line on stdin,
 *   {"id": 1, "argv": ["filesystem", "list", "--json", "--", "/my-files"]}
 * and one per line on stdout, in completion order:
 *   {"id": 1, "ok": true, "stdout": "[...]"}
 *   {"id": 1, "ok": false, "error": "Node not found: foo"}
 * Requests run concurrently. Anything not part of a reply goes to stderr.
 */

import { AsyncLocalStorage } from 'node:async_hooks';
import * as readline from 'node:readline';

import { FeatureFlags } from '@protontech/drive-sdk';

import { CliSession } from './cli';
import { AuthRequiredError, isRecoverableReplError } from './cli/errors';
import { runCommand } from './cli/run';
import { COMMANDS } from './commands';
import { init } from './init';
import { initSentry } from './telemetry';

declare const APP_VERSION: string;
declare const SDK_VERSION: string | undefined;
declare const SENTRY_DSN: string | undefined;

type Request = { id: number; argv: string[] };

const INIT_OPTIONS = {
    clientUidPrefix: 'sdk-js-cli',
    appVersion: APP_VERSION,
    sdkVersion: SDK_VERSION,
    enablePersistedEvents: true,
    enableMetrics: !!SENTRY_DSN,
    flags: {
        [FeatureFlags.DriveSmallFileUpload]: true,
    },
};

// Commands print with console.log and process.stdout.write; each request
// collects what it prints here, and the real stdout only carries replies.
const output = new AsyncLocalStorage<string[]>();
const writeStdout = process.stdout.write.bind(process.stdout);
const writeStderr = process.stderr.write.bind(process.stderr);

function capture(text: string): boolean {
    const buffer = output.getStore();
    if (buffer) {
        buffer.push(text);
    } else {
        writeStderr(text);
    }
    return true;
}

process.stdout.write = ((chunk: string | Uint8Array) =>
    capture(typeof chunk === 'string' ? chunk : new TextDecoder().decode(chunk))) as typeof process.stdout.write;
console.log = (...items: unknown[]) => {
    capture(items.map((item) => (typeof item === 'string' ? item : Bun.inspect(item))).join(' ') + '\n');
};

function reply(message: object) {
    writeStdout(JSON.stringify(message) + '\n');
}

let session: Promise<CliSession> | undefined;

function getSession(): Promise<CliSession> {
    if (!session) {
        const started = init(INIT_OPTIONS);
        session = started;
        // A failed start (e.g. offline) is retried by the next request.
        started.catch(() => {
            if (session === started) {
                session = undefined;
            }
        });
    }
    return session;
}

// `proton-drive auth login` and `logout` run as separate processes, so the
// session this worker started with can go stale. Start a fresh one when a
// command says we're signed out, and try that command once more.
async function resetSession(stale: Promise<CliSession>) {
    if (session === stale) {
        session = undefined;
        await (await stale).dispose().catch(() => undefined);
    }
}

async function run(argv: string[], retry = true): Promise<string> {
    const current = getSession();
    const buffer: string[] = [];
    try {
        await output.run(buffer, async () => runCommand(COMMANDS, ['', '', ...argv], INIT_OPTIONS, await current));
    } catch (error) {
        if (retry && error instanceof AuthRequiredError) {
            await resetSession(current);
            return run(argv, false);
        }
        throw error;
    }
    return buffer.join('');
}

async function handle({ id, argv }: Request) {
    try {
        reply({ id, ok: true, stdout: await run(argv) });
    } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        if (!isRecoverableReplError(error)) {
            console.error(error);
        }
        reply({ id, ok: false, error: message });
    }
}

initSentry({ dsn: SENTRY_DSN, appVersion: APP_VERSION, sdkVersion: SDK_VERSION });

const pending = new Set<Promise<void>>();
const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
lines.on('line', (line) => {
    if (!line.trim()) {
        return;
    }
    let request: Request;
    try {
        request = JSON.parse(line);
    } catch {
        writeStderr(`ignoring malformed request: ${line}\n`);
        return;
    }
    const task = handle(request);
    pending.add(task);
    void task.finally(() => pending.delete(task));
});
lines.on('close', async () => {
    await Promise.allSettled(pending);
    if (session) {
        await (await session).dispose().catch(() => undefined);
    }
    process.exit(0);
});
