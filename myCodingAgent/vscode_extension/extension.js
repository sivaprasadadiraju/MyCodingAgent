/**
 * extension.js - VS Code chat participant that bridges the Copilot chat
 * window to the local myCodingAgent Python agent.
 *
 * Usage in Copilot chat:
 *   @myagent build a flask todo app
 *   @myagent /ask what does app.py do
 *   @myagent /edit rename foo to bar in main.py
 */

const vscode = require('vscode');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');

const DRIVER = path.join(__dirname, 'chat_driver.py');
const approvalResolvers = new Map();

function publishApproval(request, stream, token) {
    const id = crypto.randomUUID();
    const isCommand = request.action === 'run';
    const description = isCommand
        ? '**Command approval requested**\n\n```text\n' + request.command + '\n```\n\n'
        : '**File change approval requested: `' + request.file + '`**\n\n```diff\n' +
          (request.diff || '(no textual diff)') + '\n```\n\n';
    stream.markdown(description + 'Choose an action below.');
    stream.button({
        command: 'mycodingagent.resolveApproval',
        title: isCommand ? 'Allow command' : 'Apply file change',
        arguments: [id, true]
    });
    stream.button({
        command: 'mycodingagent.resolveApproval',
        title: 'Deny',
        arguments: [id, false]
    });

    return new Promise(resolve => {
        let cancellation;
        const settle = approved => {
            approvalResolvers.delete(id);
            cancellation?.dispose();
            resolve(approved);
        };
        approvalResolvers.set(id, settle);
        cancellation = token?.onCancellationRequested(() => settle(false));
    });
}

function getConfig() {
    const cfg = vscode.workspace.getConfiguration('myCodingAgent');
    return {
        python: cfg.get('pythonPath', 'python'),
        agentFolder: cfg.get('agentFolder', ''),
        timeoutMs: (cfg.get('timeoutSeconds', 600) || 600) * 1000,
        llmBaseUrl: cfg.get('llmBaseUrl', ''),
        llmModel: cfg.get('llmModel', ''),
        llmApiKeyEnv: cfg.get('llmApiKeyEnv', 'MYCODINGAGENT_LLM_API_KEY')
    };
}

function resolveAgentFolder(configuredFolder, workspaceFolders) {
    const candidates = [];
    if (configuredFolder) candidates.push(configuredFolder);
    for (const folder of workspaceFolders || []) {
        let root = path.resolve(folder.uri.fsPath);
        for (let depth = 0; depth < 5; depth++) {
            candidates.push(path.join(root, 'myCodingAgent'), root);
            const parent = path.dirname(root);
            if (parent === root) break;
            root = parent;
        }
    }
    candidates.push(path.resolve(__dirname, '..'));
    for (const candidate of candidates) {
        const resolved = path.resolve(candidate);
        if (fs.existsSync(path.join(resolved, 'agent.py')) &&
            fs.existsSync(path.join(resolved, 'llm.py'))) return resolved;
        const nested = path.join(resolved, 'myCodingAgent');
        if (fs.existsSync(path.join(nested, 'agent.py')) &&
            fs.existsSync(path.join(nested, 'llm.py'))) return nested;
    }
    return null;
}

function validateAgentFolder(folderPath) {
    const resolved = path.resolve(folderPath);
    const hasAgent = candidate =>
        fs.existsSync(path.join(candidate, 'agent.py')) &&
        fs.existsSync(path.join(candidate, 'llm.py'));
    if (hasAgent(resolved)) return resolved;
    const nested = path.join(resolved, 'myCodingAgent');
    return hasAgent(nested) ? nested : null;
}

async function selectAgentFolder() {
    const selection = await vscode.window.showOpenDialog({
        canSelectFiles: false,
        canSelectFolders: true,
        canSelectMany: false,
        openLabel: 'Use myCodingAgent folder',
        title: 'Select folder containing myCodingAgent/agent.py'
    });
    if (!selection || !selection.length) return false;
    const folder = validateAgentFolder(selection[0].fsPath);
    if (!folder) {
        await vscode.window.showErrorMessage(
            'That folder does not contain agent.py and llm.py (or a nested myCodingAgent folder).');
        return false;
    }
    await vscode.workspace.getConfiguration('myCodingAgent').update(
        'agentFolder', folder, vscode.ConfigurationTarget.Workspace);
    return folder;
}

function parseResult(stdout) {
    const start = stdout.indexOf('MCA_RESULT_START');
    const end = stdout.indexOf('MCA_RESULT_END');
    if (start < 0 || end < 0) return null;
    try {
        return JSON.parse(stdout.slice(start + 'MCA_RESULT_START'.length, end).trim());
    } catch (_) {
        return null;
    }
}

function resolveApproval(id, approved) {
    const resolve = approvalResolvers.get(id);
    if (!resolve) return false;
    resolve(!!approved);
    return true;
}

function runAgentTask(task, mode, cwd, agentFolder, historyFile, trusted,
    stream, token) {
    return new Promise((resolve, reject) => {
        const cfg = getConfig();
        const childEnv = { ...process.env, PYTHONIOENCODING: 'utf-8' };
        if (cfg.llmBaseUrl) childEnv.MYCODINGAGENT_LLM_BASE_URL = cfg.llmBaseUrl;
        if (cfg.llmModel) childEnv.MYCODINGAGENT_LLM_MODEL = cfg.llmModel;
        const providerKey = cfg.llmApiKeyEnv ? process.env[cfg.llmApiKeyEnv] : undefined;
        if (providerKey) childEnv.MYCODINGAGENT_LLM_API_KEY = providerKey;
        else delete childEnv.MYCODINGAGENT_LLM_API_KEY;

        const proc = spawn(cfg.python, [DRIVER, '--mode', mode, '--task', task,
            '--agent-folder', agentFolder, '--history-file', historyFile,
            '--approval-stdio'], { cwd, env: childEnv });
        let stdout = '';
        let stderr = '';
        let pendingLine = '';
        let settled = false;
        const finishError = error => {
            if (!settled) { settled = true; reject(error); }
        };

        async function handleApproval(request) {
            if (request.action === 'write') {
                if (!trusted) {
                    stream.markdown('File write blocked because this workspace is not trusted.');
                    if (!proc.killed) proc.stdin.write('no\n');
                    return;
                }
                stream.progress('Applying file change...');
                stream.markdown('**Applying file change:** `' + request.file + '`\n\n' +
                    '```diff\n' + (request.diff || '(no textual diff)') + '\n```\n\n');
                if (!proc.killed) proc.stdin.write('yes\n');
                return;
            }
            const approved = trusted && await publishApproval(request, stream, token);
            if (!proc.killed) proc.stdin.write(approved ? 'yes\n' : 'no\n');
        }

        const timer = setTimeout(() => {
            proc.kill();
            finishError(new Error('myCodingAgent timed out after ' + Math.round(cfg.timeoutMs / 1000) + 's'));
        }, cfg.timeoutMs);
        token.onCancellationRequested(() => {
            clearTimeout(timer);
            for (const resolve of approvalResolvers.values()) resolve(false);
            proc.kill();
            finishError(new vscode.CancellationError());
        });
        proc.stdout.on('data', data => {
            const chunk = data.toString();
            stdout += chunk;
            pendingLine += chunk;
            const lines = pendingLine.split(/\r?\n/);
            pendingLine = lines.pop();
            for (const line of lines) {
                if (line.startsWith('MCA_APPROVAL:')) {
                    try { void handleApproval(JSON.parse(line.slice('MCA_APPROVAL:'.length))); }
                    catch (_) { if (!proc.killed) proc.stdin.write('no\n'); }
                } else if (/\[myCodingAgent step /.test(line)) {
                    stream.progress(line.trim().replace(/^\[|\]$/g, ''));
                } else if (/^\s*(read |run>|DONE:|\[fix\])/.test(line)) {
                    const status = line.trim();
                    stream.progress(status.startsWith('read ')
                        ? 'Reading workspace files...'
                        : status.startsWith('run>')
                            ? 'Running approved check...'
                            : status.startsWith('DONE:')
                                ? 'Task finished.' : 'Applying code formatting...');
                    stream.markdown('_' + status.replace(/[_*`]/g, '') + '_\n\n');
                }
            }
        });
        proc.stderr.on('data', data => { stderr += data.toString(); });
        proc.on('close', code => {
            clearTimeout(timer);
            if (settled) return;
            const result = parseResult(stdout);
            if (result) { settled = true; resolve(result); }
            else if (code !== 0) finishError(new Error('Agent exited with code ' + code + '\n' + (stderr || stdout).slice(-2000)));
            else { settled = true; resolve({ status: 'raw', reply: (stdout + '\n' + stderr).slice(-4000), files: [], commands: [] }); }
        });
        proc.on('error', error => {
            clearTimeout(timer);
            finishError(new Error('Failed to start python: ' + error.message));
        });
    });
}

function activate(context) {
    context.subscriptions.push(vscode.commands.registerCommand(
        'mycodingagent.selectAgentFolder', async () => {
            const folder = await selectAgentFolder();
            if (folder) {
                void vscode.window.showInformationMessage(
                    'myCodingAgent source set to ' + folder + '. Retry your request in Copilot Chat.');
            }
        }));
    context.subscriptions.push(vscode.commands.registerCommand(
        'mycodingagent.resolveApproval', (id, approved) => {
            resolveApproval(id, approved);
        }));
    const participant = vscode.chat.createChatParticipant('mycodingagent.agent', handler);
    const icon = path.join(__dirname, 'icon.png');
    if (fs.existsSync(icon)) participant.iconPath = vscode.Uri.file(icon);
    context.subscriptions.push(participant);

    async function handler(request, ctx, stream, token) {
        let mode = 'agent';
        if (request.command === 'ask') mode = 'ask';
        if (request.command === 'edit') mode = 'edit';
        const prompt = (request.prompt || '').trim();
        if (!prompt) {
            stream.markdown('Give me a task, e.g. `@myagent build a flask todo app` (or use `/ask`, `/edit`).');
            return;
        }
        const workspaceFolders = vscode.workspace.workspaceFolders || [];
        if (!workspaceFolders.length) {
            stream.markdown('Open the project folder in VS Code before asking myCodingAgent to work.');
            return;
        }
        if (!vscode.workspace.isTrusted) {
            stream.markdown('This workspace is not trusted by VS Code, so myCodingAgent will not run shell commands. Mark it trusted, then retry.');
            return;
        }
        const cfg = getConfig();
        const agentFolder = resolveAgentFolder(cfg.agentFolder, workspaceFolders);
        if (!agentFolder) {
            stream.markdown('Could not find the local myCodingAgent Python source. Select its folder once; the path will be saved for this workspace, then retry your request.');
            stream.button({
                command: 'mycodingagent.selectAgentFolder',
                title: 'Locate myCodingAgent source folder'
            });
            return;
        }
        const cwd = workspaceFolders[0].uri.fsPath;
        const key = crypto.createHash('sha1').update(cwd).digest('hex');
        const storageDir = context.globalStorageUri.fsPath;
        fs.mkdirSync(storageDir, { recursive: true });
        const historyFile = path.join(storageDir, key + '.chat_history.json');
        stream.progress('myCodingAgent is working...');

        try {
            const result = await runAgentTask(prompt, mode, cwd, agentFolder,
                historyFile, vscode.workspace.isTrusted, stream, token);
            if (result.files && result.files.length) {
                stream.markdown('**Files touched:** ' + result.files.map(f => '`' + f + '`').join(', ') + '\n\n');
            }
            if (result.commands && result.commands.length) {
                stream.markdown('**Commands run:** ' + result.commands.map(c => '`' + c + '`').join(', ') + '\n\n');
            }
            if (result.status === 'verification_required') {
                stream.markdown('The model service requires human verification. The agent stopped safely and cannot bypass it. Configure another provider or complete verification, then retry.');
                const open = await vscode.window.showWarningMessage(
                    'Model-service checkpoint: choose a recovery option.',
                    'Open OxAlpha Chat', 'Configure Alternate Provider');
                if (open === 'Open OxAlpha Chat') await vscode.env.openExternal(vscode.Uri.parse('https://oxalpha.com/chat'));
                else if (open === 'Configure Alternate Provider') await vscode.commands.executeCommand('workbench.action.openSettings', 'myCodingAgent.llmBaseUrl');
            } else if (result.status === 'approval_denied') {
                stream.markdown((result.reply || 'Operation declined; no change was made.') + '\n\n_File changes require diff approval. Commands run only in trusted workspaces._');
            } else if (result.status === 'needs_action') {
                stream.markdown('**No code changes were applied.** ' +
                    (result.reply || 'The model returned a response without producing a workspace edit.') +
                    '\n\nTry again using `/edit`, or configure a coding-capable model under myCodingAgent settings.');
            } else {
                stream.markdown(result.reply || '(no reply)');
            }
        } catch (error) {
            if (error instanceof vscode.CancellationError) stream.markdown('_Cancelled._');
            else stream.markdown('**Error:** ' + error.message);
        }
    }
}

function deactivate() { }
module.exports = {
    activate, deactivate, resolveAgentFolder, validateAgentFolder,
    publishApproval, resolveApproval
};
