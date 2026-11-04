using System.Collections.ObjectModel;
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

    /// <summary>引擎启动失败的原因（例如找不到 Python）。</summary>
    public static string? EngineError { get; private set; }

    /// <summary>全局运行日志。各页面往里写，只在「历史」页展示。</summary>
    public static readonly ObservableCollection<string> Log = new();
    private const int LogCap = 400;

    /// <summary>追加一条运行日志（自带上限与告警前缀），供各页面的 engine.LogReceived 调用。</summary>
    public static void AddLog(string level, string text)
    {
        Log.Add(level is "WARNING" or "ERROR" ? $"[{level}] {text}" : text);
        while (Log.Count > LogCap) Log.RemoveAt(0);
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

    /// <summary>把异常写到 %LOCALAPPDATA%\MDStudio\crash.log，方便排查。</summary>
    private static void CrashLog(Exception? ex)
    {
        try
        {
            var dir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "MDStudio");
            Directory.CreateDirectory(dir);
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
        }
        catch (Exception ex)
        {
            // 引擎起不来也要让窗口出来，界面上把原因写清楚，好排查
            EngineError = EnginePaths.InPortableLayout
                ? "找不到 Python 运行环境。\n" +
                  "请双击程序目录里的「启动.cmd」，它会装好依赖再打开本程序。\n\n" + ex.Message
                : ex.Message;
        }

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
    }
}

