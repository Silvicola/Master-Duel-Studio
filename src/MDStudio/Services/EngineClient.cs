using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace MDStudio.Services;

/// <summary>引擎返回的错误。Kind 对应 Python 侧的异常类型名。</summary>
public sealed class EngineException : Exception
{
    public string Kind { get; }

    public EngineException(string kind, string message) : base(message) => Kind = kind;
}

/// <summary>
/// 和 Python 引擎对话。标准输入输出各走一行 JSON，不用端口。
/// 生命周期跟着界面：界面关掉，引擎跟着退。
/// </summary>
public sealed class EngineClient : IAsyncDisposable
{
    private readonly Process _proc;
    private readonly SemaphoreSlim _writeLock = new(1, 1);
    private readonly ConcurrentDictionary<int, TaskCompletionSource<JsonElement>> _pending = new();
    private int _nextId;

    /// <summary>引擎日志。参数是 (级别, 文本)。</summary>
    public event Action<string, string>? LogReceived;

    /// <summary>进度。参数是 (阶段文本, 第几个, 共几个)；i 为 -1 表示不知道多久。</summary>
    public event Action<string, int, int>? ProgressChanged;

    private EngineClient(Process proc)
    {
        _proc = proc;
    }

    /// <summary>
    /// 启动引擎。pythonExe / engineDir / coreDir 用绝对路径。
    /// coreDir 是 mdstudio 包的 src 目录，PYTHONPATH 要带上它。
    /// </summary>
    public static EngineClient Start(string pythonExe, string engineDir, string coreDir)
    {
        var psi = new ProcessStartInfo
        {
            FileName = pythonExe,
            WorkingDirectory = engineDir,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = new UTF8Encoding(false),
            StandardInputEncoding = new UTF8Encoding(false),
        };
        psi.ArgumentList.Add("-u");
        psi.ArgumentList.Add("-m");
        psi.ArgumentList.Add("mdanim_engine");
        psi.Environment["PYTHONPATH"] = engineDir + Path.PathSeparator + coreDir;
        psi.Environment["PYTHONIOENCODING"] = "utf-8";
        psi.Environment["PYTHONUNBUFFERED"] = "1";

        var proc = Process.Start(psi) ?? throw new InvalidOperationException("引擎启动失败");
        var client = new EngineClient(proc);
        client.StartReading();
        return client;
    }

    private void StartReading()
    {
        _ = Task.Run(async () =>
        {
            // stderr 是引擎自己的日志，单独读走，不要和协议混在一起
            _ = Task.Run(async () =>
            {
                while (await _proc.StandardError.ReadLineAsync() is { } err)
                {
                    Debug.WriteLine("[engine] " + err);
                    // Python 的 traceback 只走 stderr，不接到界面日志里的话报错就查不到原因
                    LogReceived?.Invoke("STDERR", err);
                }
            });

            while (await _proc.StandardOutput.ReadLineAsync() is { } line)
            {
                if (string.IsNullOrWhiteSpace(line)) continue;
                JsonElement root;
                try
                {
                    using var doc = JsonDocument.Parse(line);
                    root = doc.RootElement.Clone();
                }
                catch (JsonException)
                {
                    Debug.WriteLine("[engine] 协议行解析失败: " + line);
                    continue;
                }

                if (root.TryGetProperty("ev", out var ev))
                {
                    DispatchEvent(ev.GetString(), root);
                    continue;
                }

                if (root.TryGetProperty("id", out var idEl) && idEl.ValueKind == JsonValueKind.Number
                    && _pending.TryRemove(idEl.GetInt32(), out var tcs))
                {
                    tcs.TrySetResult(root);
                }
            }

            // 读到头 = 引擎没了，把所有等待中的调用叫醒
            foreach (var kv in _pending)
                kv.Value.TrySetException(new EngineException("Exited", "引擎已退出"));
            _pending.Clear();
        });
    }

    private void DispatchEvent(string? kind, JsonElement root)
    {
        switch (kind)
        {
            case "log":
                LogReceived?.Invoke(
                    root.TryGetProperty("level", out var lv) ? lv.GetString() ?? "INFO" : "INFO",
                    root.TryGetProperty("text", out var tx) ? tx.GetString() ?? "" : "");
                break;
            case "progress":
                ProgressChanged?.Invoke(
                    root.TryGetProperty("text", out var pt) ? pt.GetString() ?? "" : "",
                    root.TryGetProperty("i", out var pi) ? pi.GetInt32() : -1,
                    root.TryGetProperty("n", out var pn) ? pn.GetInt32() : 0);
                break;
        }
    }

    /// <summary>发一条命令并等应答。引擎报错时抛 EngineException。</summary>
    public async Task<JsonElement> CallAsync(string op, object? args = null,
        CancellationToken ct = default)
    {
        var id = Interlocked.Increment(ref _nextId);
        var tcs = new TaskCompletionSource<JsonElement>(TaskCreationOptions.RunContinuationsAsynchronously);
        _pending[id] = tcs;

        var payload = new JsonObject
        {
            ["id"] = id,
            ["op"] = op,
            ["args"] = JsonSerializer.SerializeToNode(args),
        };

        await _writeLock.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            await _proc.StandardInput.WriteLineAsync(payload.ToJsonString()).ConfigureAwait(false);
            await _proc.StandardInput.FlushAsync().ConfigureAwait(false);
        }
        finally
        {
            _writeLock.Release();
        }

        var root = await tcs.Task.WaitAsync(ct).ConfigureAwait(false);
        if (root.TryGetProperty("ok", out var okEl) && okEl.GetBoolean())
            return root.TryGetProperty("data", out var data) ? data : default;

        var err = root.TryGetProperty("error", out var e) ? e : default;
        throw new EngineException(
            err.ValueKind == JsonValueKind.Object && err.TryGetProperty("kind", out var k)
                ? k.GetString() ?? "Error" : "Error",
            err.ValueKind == JsonValueKind.Object && err.TryGetProperty("message", out var m)
                ? m.GetString() ?? "未知错误" : "未知错误");
    }

    public ValueTask DisposeAsync()
    {
        try
        {
            if (!_proc.HasExited)
            {
                _proc.StandardInput.WriteLine("{\"id\":0,\"op\":\"shutdown\",\"args\":{}}");
                _proc.StandardInput.Flush();
                if (!_proc.WaitForExit(1500)) _proc.Kill(true);
            }
        }
        catch
        {
            // 关窗时引擎已经没了也无所谓
        }
        finally
        {
            _proc.Dispose();
        }
        return ValueTask.CompletedTask;
    }
}
