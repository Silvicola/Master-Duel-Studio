using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media.Imaging;
using MDStudio.Pages;
using System.Text.Json;
using Windows.Graphics;
using Windows.System;

// To learn more about WinUI, the WinUI project structure,
// and more about our project templates, see: http://aka.ms/winui-project-info.

namespace MDStudio;

public sealed partial class MainWindow : Window
{
    public MainWindow()
    {
        InitializeComponent();

        ExtendsContentIntoTitleBar = true;
        SetTitleBar(AppTitleBar);
        AppWindow.TitleBar.PreferredHeightOption = TitleBarHeightOption.Tall;

        // Esc 关预览、空格播放暂停。挂到根 Grid：Window 本身不是 UIElement
        Shell.AddHandler(UIElement.KeyDownEvent, new KeyEventHandler(Shell_KeyDown), true);

        // 标题条右侧跟着播放进度显示「当前帧 / 总帧」。控件是单实例，挂一次即可
        PreviewView.FrameChanged += OnPreviewFrameChanged;

        // 每落到一个新页面就重查一次前置状态：账号可能刚在设置里改过，也可能刚被
        // 游戏更新弄没了。设置页先放行，那里是修问题的地方，不能挡。
        NavFrame.Navigated += (_, e) =>
        {
            _allowBlock = e.SourcePageType != typeof(SettingsPage);
            ApplyReadinessState();
            _ = RefreshReadinessAsync();
        };

        // 图标一律用 exe 所在目录的绝对路径.
        // XAML 里的 ImageSource="Assets\AppIcon.ico" 是相对路径, 跟着当前工作目录走:
        // 开发时 dotnet run 的工作目录是项目目录所以能显示, 发到别处就空了.
        var assets = Path.Combine(AppContext.BaseDirectory, "Assets");
        var icoPath = Path.Combine(assets, "AppIcon.ico");
        if (File.Exists(icoPath))
        {
            AppWindow.SetIcon(icoPath);
        }
        var pngPath = Path.Combine(assets, "AppIcon.png");
        if (File.Exists(pngPath))
        {
            // 注意属性名是 ImageSource, 不是 Bitmap / BitmapSource.
            // 用 PNG 而不是 ICO: BitmapImage 解 ICO 不稳, 会静默加载失败.
            AppTitleBar.IconSource = new ImageIconSource
            {
                ImageSource = new BitmapImage(new Uri(pngPath)),
            };
        }

        // Resize 吃物理像素: 150% 缩放下给 1280 只剩 853 逻辑宽，展开导航后两栏放不下。
        // 按缩放比换算成逻辑尺寸，再用工作区大小兜底，免得一开窗就顶满屏幕。
        if (Content is FrameworkElement root)
        {
            root.Loaded += (_, _) =>
            {
                var scale = root.XamlRoot?.RasterizationScale ?? 1.0;
                var area = DisplayArea.GetFromWindowId(AppWindow.Id,
                    DisplayAreaFallback.Primary).WorkArea;
                var w = Math.Min((int)(1200 * scale), area.Width);
                var h = Math.Min((int)(780 * scale), area.Height);
                // 只 Resize 不改位置会让窗口挂在屏幕外，一起居中
                AppWindow.MoveAndResize(new RectInt32(
                    area.X + (area.Width - w) / 2,
                    area.Y + (area.Height - h) / 2,
                    w, h));

                // 导航栏默认收成一条图标. XAML 里写 IsPaneOpen="False" 不管用,
                // 模板应用完会被重置, 只能在运行时设.
                NavView.IsPaneOpen = false;

                // 初始页面是设置页：进软件先看当前账号、组件状态、卡片数据版本，
                // 这三样各发一条查询就有结果。以前默认落在卡图页，那一页一进来
                // 就发 cards.search，冷启动要等索引建完（实测 4 秒起），
                // 用户先看到的是一整片转圈。
                //
                // 必须延后一拍：Loaded 里直接设会被 NavigationView 自己的首项
                // 默认选中覆盖掉（实测设了没用，落地还是卡图页）。排到下一轮
                // 调度，等它把默认项应用完再改。
                DispatcherQueue.TryEnqueue(() =>
                    NavView.SelectedItem = NavView.SettingsItem);
            };
        }

        // 初始页面交给上面 Loaded 里的设置项，不再由 XAML 的 IsSelected 决定：
        // 那个属性模板应用后会被重置，写了也不一定生效。
    }

    private void TitleBar_PaneToggleRequested(TitleBar sender, object args)
    {
        NavView.IsPaneOpen = !NavView.IsPaneOpen;
    }

    /// <summary>预演开关在设置页改了, 同步标题栏那个胶囊。</summary>
    public void UpdateDryRunBadge()
    {
        DryRunBadge.Visibility = App.DryRun ? Visibility.Visible : Visibility.Collapsed;
    }

    private void DryRunBadge_Click(object sender, RoutedEventArgs e)
    {
        App.DryRun = false;
        UpdateDryRunBadge();
    }

    /// <summary>
    /// 查一次前置状态（游戏目录、账号），决定内容区要不要换成提示页。
    /// 走廉价查询：不建卡索引、不定位登记表，切页调它不会拖慢导航。
    /// 引擎一时答不上来就不挡：把界面锁死在一次失败上，比漏挡更糟。
    /// </summary>
    public async Task RefreshReadinessAsync()
    {
        if (App.Engine is not { } engine || _readinessBusy) return;
        _readinessBusy = true;
        try
        {
            ApplyReadiness(await engine.CallAsync("app.ready"));
        }
        catch
        {
            // 静默：下一次导航或账号变更会再查一遍
        }
        finally
        {
            _readinessBusy = false;
        }
    }

    private bool _readinessBusy;
    /// <summary>当前页面允许被这层提示挡住（设置页不允许，那里是修问题的地方）。</summary>
    private bool _allowBlock;
    // 上一次查到的状态。切页时先用它决定，避免旧内容在查询回来之前闪一下；
    // 查询回来再纠正。没查过时按「正常」处理，宁可漏挡一次，也不误锁界面。
    private bool _gameOk = true, _accOk = true, _accMissing;

    private void ApplyReadiness(JsonElement r)
    {
        _gameOk = Flag(r, "game_dir_ok");
        _accOk = Flag(r, "account_ok");
        _accMissing = Flag(r, "account_missing");
        ApplyReadinessState();
    }

    private void ApplyReadinessState()
    {
        if (!_allowBlock || (_gameOk && _accOk))
        {
            AccountBlock.Visibility = Visibility.Collapsed;
            NavFrame.Visibility = Visibility.Visible;
            return;
        }

        if (!_gameOk)
        {
            BlockTitle.Text = "找不到游戏目录";
            BlockBody.Text = "软件要靠游戏文件才能工作。请到设置里指定 Master Duel 的安装位置。";
            BlockGo.Content = "去设置指定";
        }
        else if (_accMissing)
        {
            BlockTitle.Text = "选定的账号暂时不可用";
            BlockBody.Text = "那个账号目录现在找不到，游戏可能正在更新。等它更新完再回来，或者到设置里另选一个。";
            BlockGo.Content = "去设置查看";
        }
        else
        {
            BlockTitle.Text = "还没有选择账号";
            BlockBody.Text = "一个游戏目录下可能有多个账号，各自是一份独立的游戏文件。选定之后才能读取卡片和历史。";
            BlockGo.Content = "去设置选择";
        }
        AccountBlock.Visibility = Visibility.Visible;
        // 把页面整个收起来而不是盖一层不透明色块：底就是 Mica，和别的页同一种底，
        // 也不会残留旧内容透出来的问题
        NavFrame.Visibility = Visibility.Collapsed;
    }

    private static bool Flag(JsonElement r, string key)
        => r.ValueKind == JsonValueKind.Object && r.TryGetProperty(key, out var v)
           && v.ValueKind == JsonValueKind.True;

    private void BlockGo_Click(object sender, RoutedEventArgs e) => GoToSettings();

    /// <summary>
    /// 跳到设置页；给了 key 就滚到对应组件并闪一下边框。
    ///
    /// 设置页是 NavigationView 的内置项，设 SelectedItem 会触发导航；
    /// 如果本来就在设置页，直接拿当前实例定位。
    /// </summary>
    public void GoToSettings(string? focus = null)
    {
        NavView.SelectedItem = NavView.SettingsItem;

        if (NavFrame.Content is SettingsPage page)
        {
            page.FocusAsset(focus);
            return;
        }
        // 导航理论上同步完成，保险起见再补一次
        if (focus is not null)
            DispatcherQueue.TryEnqueue(() =>
            {
                if (NavFrame.Content is SettingsPage late) late.FocusAsset(focus);
            });
    }

    private void NavView_SelectionChanged(NavigationView sender, NavigationViewSelectionChangedEventArgs args)
    {
        if (args.IsSettingsSelected)
        {
            NavFrame.Navigate(typeof(SettingsPage));
        }
        else if (args.SelectedItem is NavigationViewItem item)
        {
            switch (item.Tag)
            {
                case "cardart":
                    NavFrame.Navigate(typeof(CardArtPage));
                    break;
                case "anim":
                    NavFrame.Navigate(typeof(AnimPage));
                    break;
                case "wallpaper":
                    NavFrame.Navigate(typeof(WallpaperPage));
                    break;
                case "share":
                    NavFrame.Navigate(typeof(SharePage));
                    break;
                case "history":
                    NavFrame.Navigate(typeof(HistoryPage));
                    break;
                default:
                    throw new InvalidOperationException($"Unknown navigation item tag: {item.Tag}");
            }
        }
    }

    // ---------------------------------------------------------------- 动画预览浮层

    private const double SmallPreviewWidth = 880;
    private const double SmallPreviewHeight = 601;   // 44 标题条 + 557 播放器
    private bool _previewOpen;
    private int _previewFrames;
    private double _previewSeconds;
    private string _previewKind = "cutin";

    /// <summary>
    /// 弹出动画预览。成功返回 null，失败返回一句给用户看的提示。
    /// 浮层挂在窗口上，所以任何页面都能叫它，切页也不会把它带走。
    /// </summary>
    public async Task<string?> ShowCutinPreviewAsync(int card, string title, string subtitle,
                                                    string kind = "cutin")
    {
        if (App.Engine is not { } engine) return "引擎没有启动。";
        kind = kind == "runfx" ? "runfx" : "cutin";
        JsonElement meta;
        try
        {
            meta = await engine.CallAsync("preview.meta", new { card, kind });
        }
        catch (Exception ex)
        {
            return "读取预览失败：" + ex.Message;
        }
        if (meta.ValueKind != JsonValueKind.Object ||
            !meta.TryGetProperty("frames", out var framesEl) || framesEl.GetInt32() <= 0)
            return "这张卡的预览已删除，重新生成后才有。";

        int count = framesEl.GetInt32();
        double dur = meta.TryGetProperty("duration", out var dEl) ? dEl.GetDouble() : 0;
        _previewKind = kind;
        PreviewTitle.Text = string.IsNullOrEmpty(title) ? $"#{card}" : title;
        PreviewSubtitle.Text = subtitle;
        _previewFrames = count;
        _previewSeconds = dur;
        PreviewMeta.Text = MetaText(1);
        PreviewHost.Width = SmallPreviewWidth;
        PreviewHost.Height = SmallPreviewHeight;
        PreviewLayer.Visibility = Visibility.Visible;
        _previewOpen = true;
        if (await PreviewView.OpenAsync(card, kind)) return null;
        CloseCutinPreview();
        return "预览打不开，重新生成一次再看。";
    }

    private void OnPreviewFrameChanged(int current, int total)
    {
        if (!_previewOpen) return;
        if (total > 0) _previewFrames = total;
        PreviewMeta.Text = MetaText(current);
    }

    /// <summary>「12 / 48 帧 · 1.6 秒」。当前帧没拿到时不带帧号，只报总量。</summary>
    private string MetaText(int current)
    {
        var kind = _previewKind == "runfx" ? "特效" : "动画";
        var text = current > 0 && _previewFrames > 0
            ? $"{kind} · {current} / {_previewFrames} 帧"
            : $"{kind} · {_previewFrames} 帧";
        return _previewSeconds > 0 ? $"{text} · {_previewSeconds:0.0} 秒" : text;
    }

    public void CloseCutinPreview()
    {
        if (!_previewOpen) return;
        _previewOpen = false;
        PreviewView.Close();
        PreviewLayer.Visibility = Visibility.Collapsed;
    }

    private void PreviewClose_Click(object sender, RoutedEventArgs e) => CloseCutinPreview();

    private void Shell_KeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (!_previewOpen) return;
        if (e.Key == VirtualKey.Escape)
        {
            e.Handled = true;
            CloseCutinPreview();
            return;
        }
        if (e.Key == VirtualKey.Space)
        {
            e.Handled = true;
            PreviewView.TogglePlay();
        }
    }
}
