using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media.Imaging;
using MDStudio.Pages;
using Windows.Graphics;

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
            };
        }

        // 初始页面由 XAML 里那个 IsSelected="True" 触发导航. 这里再 Navigate 一次会被
        // SelectionChanged 覆盖, 白跑一趟.
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
}
