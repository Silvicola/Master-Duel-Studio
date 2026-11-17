using System;
using System.Collections.ObjectModel;
using System.IO;
using System.Text;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Data;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Navigation;
using MDStudio.Services;

// To learn more about WinUI, the WinUI project structure,
// and more about our project templates, see: http://aka.ms/winui-project-info.

namespace MDStudio;

/// <summary>
/// Provides application-specific behavior to supplement the default Application class.
/// </summary>
public partial class App : Application
{
    private Window? _window;

    /// <summary>整个应用共用一个 Python 引擎进程。</summary>
    public static EngineClient? Engine { get; private set; }

    /// <summary>主窗口。文件选择器要拿它换 HWND。</summary>
    public static Window? MainWindow { get; private set; }

    /// <summary>
    /// 预演模式：只跑流程不写游戏文件。只在本次运行有效。
    /// 走属性而不是裸字段，是为了变更时能通知界面——标题栏徽标和设置页的开关
    /// 是两个入口，改一个另一个必须跟着动。
    /// </summary>
    public static bool DryRun
    {
        get => _dryRun;
        set
        {
            if (_dryRun == value) return;
            _dryRun = value;
            DryRunChanged?.Invoke();
        }
    }
    private static bool _dryRun;

    /// <summary>预演开关变化时触发（徽标点击、设置页开关切换都会走这里）。</summary>
    public static event Action? DryRunChanged;

    /// <summary>主任务槽是否有动作在跑（写游戏文件的那种）。组件下载不算。</summary>
    public static bool MainBusy { get; private set; }

    /// <summary>主任务槽进出时触发。注意：来自引擎读线程，改界面要自己转回 UI 线程。</summary>
    public static event Action? MainBusyChanged;

    /// <summary>引擎启动失败的原因（例如找不到 Python）。</summary>
    public static string? EngineError { get; private set; }

    /// <summary>全局运行日志。只在历史页「复制日志」时读取，界面不再展示正文。</summary>
    public static readonly ObservableCollection<string> Log = new();
    private const int LogCap = 400;
    private static readonly object LogLock = new();

    /// <summary>本次运行的日志文件（按天一个，实时追加，重启不清）。</summary>
    public static string LogFilePath { get; private set; } = "";

    /// <summary>取一份日志快照，供复制到剪贴板。</summary>
    public static string LogSnapshot()
    {
        lock (LogLock) return string.Join("\r\n", Log);
    }

    /// <summary>追加一条运行日志，同时落盘。引擎日志由本类单点订阅，页面不再各自登记。</summary>
    public static void AddLog(string level, string text)
    {
        var line = level is "WARNING" or "ERROR" ? $"[{level}] {text}" : text;
        lock (LogLock)
        {
            Log.Add(line);
            while (Log.Count > LogCap) Log.RemoveAt(0);
            try
            {
                var dir = Path.Combine(AppContext.BaseDirectory, "data", "logs");
                Directory.CreateDirectory(dir);
                LogFilePath = Path.Combine(dir, $"运行日志_{DateTime.Now:yyyyMMdd}.txt");
                File.AppendAllText(LogFilePath,
                    $"{DateTime.Now:HH:mm:ss} {line}{Environment.NewLine}", Encoding.UTF8);
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
    }

    /// <summary>
    /// Initializes the singleton application object.  This is the first line of authored code
    /// executed, and as such is the logical equivalent of main() or WinMain().
    /// </summary>
    public App()
    {
        InitializeComponent();

        // XAML 层的崩溃默认什么都不留下，只能靠这个把原因写进文件
        UnhandledException += (_, e) =>
        {
            CrashLog(e.Exception);
            e.Handled = false;
        };
    }

    /// <summary>
    /// 把异常写到程序目录下的 crash.log，方便排查。
    ///
    /// 放在程序目录而不是 %LOCALAPPDATA%：整个工具的原则是配置、数据、日志都跟着
    /// exe 走，用户把文件夹搬到别处能一起带走，也不往系统目录里留东西。
    /// </summary>
    private static void CrashLog(Exception? ex)
    {
        try
        {
            var dir = Path.GetDirectoryName(Environment.ProcessPath) ?? AppContext.BaseDirectory;
            File.AppendAllText(Path.Combine(dir, "crash.log"),
                $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss}] {ex}\r\n\r\n");
        }
        catch
        {
            // 日志都写不了就没办法了
        }
    }

    /// <summary>
    /// Invoked when the application is launched.
    /// </summary>
    /// <param name="args">Details about the launch request and process.</param>
    protected override void OnLaunched(Microsoft.UI.Xaml.LaunchActivatedEventArgs args)
    {
        try
        {
            Engine = EngineClient.Start(EnginePaths.Python, EnginePaths.EngineDir, EnginePaths.Core);
            // 日志单点订阅：以前动画页/分享页/历史页各自订阅，同一条日志会被
            // 重复追加两三行。收敛到这里，各页面退订，日志不重复也不丢。
            if (Engine is { } engine)
            {
                engine.LogReceived += (level, text) => AddLog(level, text);
                // 有动作在跑时锁住账号与游戏目录：引擎那边也会拒，这里提前把按钮灰掉
                engine.MainBusyChanged += busy =>
                {
                    MainBusy = busy;
                    MainBusyChanged?.Invoke();
                };
            }
        }
        catch (Exception ex)
        {
            // 引擎起不来也要让窗口出来，界面上把原因写清楚，好排查
            EngineError = EnginePaths.InPortableLayout
                ? "找不到 Python 运行环境。\n" +
                  "请双击程序目录里的「启动.cmd」，它会装好依赖再打开本程序。\n\n" + ex.Message
                : ex.Message;
        }

        _ = StartAsync();
    }

    /// <summary>
    /// 直接开主窗口。
    ///
    /// 早先这里先过一道资源门禁（缺模型就不让进），但那些组件只影响个别功能
    /// （AI 抠图、超分、Spine 版本转换），为它们把整个软件挡在门外不划算。
    /// 现在改成：进得去，用到时才提示，去设置页按需下载。
    /// </summary>
    private Task StartAsync()
    {
        try
        {
            _window = new MainWindow();
            MainWindow = _window;
            _window.Closed += async (_, _) =>
            {
                MainWindow = null;
                if (Engine is { } engine) await engine.DisposeAsync();
            };
            _window.Activate();
        }
        catch (Exception ex)
        {
            // XAML 加载失败会在这里现形，光看事件日志只有一句 stowed exception
            CrashLog(ex);
            throw;
        }
        return Task.CompletedTask;
    }
}

