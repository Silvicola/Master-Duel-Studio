using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using System.Threading.Tasks;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using Windows.Storage.Pickers;

namespace MDStudio.Pages;

/// <summary>
/// 壁纸页：左边换大厅壁纸（6aadf393 的 WallPaper0001_1），右边换大厅背景（6 个 ShopBGBase）。
/// 两栏各自独立：选图 + 确认替换 + 还原。写入走引擎的 wallpaper.apply / restore。
/// </summary>
public sealed partial class WallpaperPage : Page
{
    private string? _pickedLobby;
    private string? _pickedShop;
    private bool _lobbyReplaced;
    private bool _shopReplaced;
    private bool _busyLobby;           // 左栏（大厅壁纸）是否正在操作
    private bool _busyShop;            // 右栏（大厅背景）是否正在操作
    private double[]? _lobbyScreen;    // 游戏屏幕 [宽,高]
    private double[]? _lobbyTexRect;   // 大厅壁纸纹理在屏幕坐标系里的外接矩形 [x,y,w,h]
    private BitmapImage? _lobbyNowBmp; // 游戏当前壁纸位图
    private BitmapImage? _lobbyPickBmp;// 用户选中的壁纸位图
    private BitmapImage? _pickedShopBmp; // 用户选中的大厅背景位图（三行三列的第 9 格）
    private JsonElement _shopPrev;       // 最近一次拉到的 8 张背景预览，选图后重渲染用

    public WallpaperPage()
    {
        InitializeComponent();
        Loaded += WallpaperPage_Loaded;
        LobbyNowCanvas.SizeChanged += (_, _) => LayoutLobby();
        LobbyPickCanvas.SizeChanged += (_, _) => LayoutLobby();
    }

    private async void WallpaperPage_Loaded(object sender, RoutedEventArgs e) => await RefreshAsync();

    /// <summary>拉状态 + 大厅壁纸预览 + 6 张背景缩略图。</summary>
    private async Task RefreshAsync()
    {
        if (App.Engine is not { } engine) return;

        await GuardAsync("读取壁纸状态", async () =>
        {
            var st = await engine.CallAsync("wallpaper.status");
            if (st.ValueKind == JsonValueKind.Object)
            {
                _lobbyReplaced = Flag(st, "lobby");
                _shopReplaced = Flag(st, "shop");
                if (st.TryGetProperty("lobby_screen", out var sc) &&
                    sc.ValueKind == JsonValueKind.Array && sc.GetArrayLength() == 2)
                {
                    _lobbyScreen = new[] { sc[0].GetDouble(), sc[1].GetDouble() };
                }
                if (st.TryGetProperty("lobby_tex_rect", out var tr) &&
                    tr.ValueKind == JsonValueKind.Array && tr.GetArrayLength() == 4)
                {
                    _lobbyTexRect = new[]
                    {
                        tr[0].GetDouble(), tr[1].GetDouble(),
                        tr[2].GetDouble(), tr[3].GetDouble(),
                    };
                }
            }
        });

        await GuardAsync("读取大厅壁纸预览", async () =>
        {
            var lobby = await engine.CallAsync("wallpaper.preview",
                new Dictionary<string, object?> { ["which"] = "lobby" });
            if (lobby.ValueKind != JsonValueKind.Object) return;
            var p = Str(lobby, "path");
            if (p.Length > 0 && File.Exists(p)) _lobbyNowBmp = await LoadBitmapAsync(p);
            LobbyMeta.Text = $"6aadf393（青眼亚白龙） · {Str(lobby, "name")} · {Num(lobby, "width")}×{Num(lobby, "height")}";
        });

        await GuardAsync("读取大厅背景预览", async () =>
        {
            var arr = await engine.CallAsync("wallpaper.previews",
                new Dictionary<string, object?> { ["which"] = "shop" });
            if (arr.ValueKind == JsonValueKind.Array)
            {
                _shopPrev = arr;
                await FillShopTilesAsync(arr);
            }
        });

        LayoutLobby();
        if (!_busyLobby && !_busyShop) SetState();
    }

    /// <summary>右侧 8 张游戏背景 + 1 张你的图 = 三行三列，白细线框。</summary>
    private async Task FillShopTilesAsync(JsonElement arr)
    {
        const int cols = 3;
        ShopTiles.Children.Clear();
        ShopTiles.RowDefinitions.Clear();
        ShopTiles.ColumnDefinitions.Clear();
        for (int c = 0; c < cols; c++)
            ShopTiles.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

        int n = arr.GetArrayLength() + (_pickedShopBmp is not null ? 1 : 0);
        int rows = Math.Max(1, (n + cols - 1) / cols);
        for (int r = 0; r < rows; r++)
            ShopTiles.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

        int i = 0;
        foreach (var item in arr.EnumerateArray())
        {
            var caption = $"{Str(item, "bundle")} · {Str(item, "name")}";
            var cell = await MakeTileAsync(caption, Str(item, "path"));
            Grid.SetRow(cell, i / cols);
            Grid.SetColumn(cell, i % cols);
            ShopTiles.Children.Add(cell);
            i++;
        }
        if (_pickedShopBmp is not null)
        {
            var cell = MakeTile("你的图片 · 1920×1080", _pickedShopBmp);
            Grid.SetRow(cell, i / cols);
            Grid.SetColumn(cell, i % cols);
            ShopTiles.Children.Add(cell);
        }
    }

    private async Task<StackPanel> MakeTileAsync(string caption, string path)
    {
        BitmapImage? bmp = null;
        if (path.Length > 0 && File.Exists(path)) bmp = await LoadBitmapAsync(path);
        return MakeTile(caption, bmp);
    }

    private static StackPanel MakeTile(string caption, BitmapImage? bmp)
    {
        var cell = new StackPanel { Spacing = 4 };
        var frame = new Border
        {
            Height = 76,
            Background = new SolidColorBrush(Microsoft.UI.Colors.Transparent),
            BorderBrush = new SolidColorBrush(Microsoft.UI.Colors.White),
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(4),
        };
        frame.Child = new Image { Stretch = Stretch.Uniform, Margin = new Thickness(1), Source = bmp };
        var cap = new TextBlock
        {
            Text = caption,
            FontSize = 11,
            Opacity = 0.75,
            TextTrimming = TextTrimming.CharacterEllipsis,
        };
        cell.Children.Add(frame);
        cell.Children.Add(cap);
        return cell;
    }

    private void SetState()
    {
        LobbyRestoreBtn.IsEnabled = !_busyLobby && _lobbyReplaced;
        ShopRestoreBtn.IsEnabled = !_busyShop && _shopReplaced;
        LobbyApplyBtn.IsEnabled = !_busyLobby;
        ShopApplyBtn.IsEnabled = !_busyShop;
    }

    // ----------------------------------------------------------------- 选图
    private async void LobbyPickBtn_Click(object sender, RoutedEventArgs e)
    {
        var path = await PickImageAsync();
        if (path is null) return;
        _pickedLobby = path;
        LobbyPickBox.Text = Path.GetFileName(path);
        _lobbyPickBmp = await LoadBitmapAsync(path);
        LayoutLobby();
        SetLobbyStatus("");
    }

    private async void ShopPickBtn_Click(object sender, RoutedEventArgs e)
    {
        var path = await PickImageAsync();
        if (path is null) return;
        _pickedShop = path;
        ShopPickBox.Text = Path.GetFileName(path);
        _pickedShopBmp = await LoadBitmapAsync(path);
        // 重渲染三行三列：第 9 格显示刚选的图
        if (_shopPrev.ValueKind == JsonValueKind.Array) await FillShopTilesAsync(_shopPrev);
        SetShopStatus("");
    }

    private async Task<string?> PickImageAsync()
    {
        var picker = new FileOpenPicker
        {
            SuggestedStartLocation = PickerLocationId.PicturesLibrary,
            ViewMode = PickerViewMode.Thumbnail,
        };
        foreach (var ext in new[] { ".png", ".webp", ".jpg", ".jpeg" })
            picker.FileTypeFilter.Add(ext);

        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(App.MainWindow);
        WinRT.Interop.InitializeWithWindow.Initialize(picker, hwnd);
        var file = await picker.PickSingleFileAsync();
        return file?.Path;
    }

    // ----------------------------------------------------------------- 替换 / 还原
    private async void LobbyApplyBtn_Click(object sender, RoutedEventArgs e)
    {
        if (App.Engine is not { } engine) return;
        if (_pickedLobby is null) { SetLobbyStatus("先选一张图片"); return; }
        SetLobbyBusy(true, "正在替换大厅壁纸…");
        try
        {
            await engine.CallAsync("wallpaper.apply", new Dictionary<string, object?>
            {
                ["image"] = _pickedLobby,
                ["which"] = new[] { "lobby" },
            });
            SetLobbyBusy(false, "");
            await RefreshAsync();
            SetLobbyStatus("已替换大厅壁纸。完全退出游戏再重开才会加载。");
        }
        catch (Exception ex) { SetLobbyBusy(false, ""); ReportError("替换大厅壁纸失败", ex); }
    }

    private async void ShopApplyBtn_Click(object sender, RoutedEventArgs e)
    {
        if (App.Engine is not { } engine) return;
        if (_pickedShop is null) { SetShopStatus("先选一张图片"); return; }
        SetShopBusy(true, "正在替换大厅背景…");
        try
        {
            await engine.CallAsync("wallpaper.apply", new Dictionary<string, object?>
            {
                ["image"] = _pickedShop,
                ["which"] = new[] { "shop" },
            });
            SetShopBusy(false, "");
            await RefreshAsync();
            SetShopStatus("已替换 8 张大厅背景（含 data.unity3d 的 ShopBGBase02、TexSoloTransitionBG）。完全退出游戏再重开才会加载。");
        }
        catch (Exception ex) { SetShopBusy(false, ""); ReportError("替换大厅背景失败", ex); }
    }

    private async void LobbyRestoreBtn_Click(object sender, RoutedEventArgs e)
    {
        if (App.Engine is not { } engine) return;
        SetLobbyBusy(true, "正在还原大厅壁纸…");
        try
        {
            await engine.CallAsync("wallpaper.restore", new Dictionary<string, object?>
            {
                ["which"] = new[] { "lobby" },
            });
            SetLobbyBusy(false, "");
            await RefreshAsync();
            SetLobbyStatus("已还原成游戏原图。");
        }
        catch (Exception ex) { SetLobbyBusy(false, ""); ReportError("还原大厅壁纸失败", ex); }
    }

    private async void ShopRestoreBtn_Click(object sender, RoutedEventArgs e)
    {
        if (App.Engine is not { } engine) return;
        SetShopBusy(true, "正在还原大厅背景…");
        try
        {
            await engine.CallAsync("wallpaper.restore", new Dictionary<string, object?>
            {
                ["which"] = new[] { "shop" },
            });
            SetShopBusy(false, "");
            await RefreshAsync();
            SetShopStatus("已还原成游戏原图。");
        }
        catch (Exception ex) { SetShopBusy(false, ""); ReportError("还原大厅背景失败", ex); }
    }

    private async void ReloadBtn_Click(object sender, RoutedEventArgs e)
    {
        SetLobbyBusy(true, "");
        SetShopBusy(true, "");
        await RefreshAsync();
        SetLobbyBusy(false, "");
        SetShopBusy(false, "");
    }

    // ----------------------------------------------------------------- 预览框
    /// <summary>
    /// 在大厅壁纸预览画布里画「2560×1440 屏幕框 + 整张 2048×1304 纹理(红框) + 图片」。
    /// 纹理按游戏真实摆放（放大 4/3、向右上偏移）放进去，所以红框比屏幕框大且偏右，
    /// 直观展示这张图在屏幕里占哪个位置、占多少（溢出部分即被裁掉/不显示）。
    /// </summary>
    private void DrawScene(Canvas cv, BitmapImage? bmp)
    {
        cv.Children.Clear();
        if (_lobbyScreen is null || _lobbyTexRect is null) return;
        double sw = _lobbyScreen[0], sh = _lobbyScreen[1];
        double tx = _lobbyTexRect[0], ty = _lobbyTexRect[1],
               tw = _lobbyTexRect[2], th = _lobbyTexRect[3];

        // 场景边界 = 屏幕框 与 纹理外接矩形 的并集
        double minX = Math.Min(0, tx), minY = Math.Min(0, ty);
        double maxX = Math.Max(sw, tx + tw), maxY = Math.Max(sh, ty + th);
        double sceneW = maxX - minX, sceneH = maxY - minY;

        double cw = cv.ActualWidth, ch = cv.ActualHeight;
        if (cw <= 1 || ch <= 1) return;
        const double pad = 6;
        double k = Math.Min((cw - 2 * pad) / sceneW, (ch - 2 * pad) / sceneH);
        double ox = (cw - sceneW * k) / 2 - minX * k;   // 场景 x=0 对应的画布 x
        double oy = (ch - sceneH * k) / 2 - minY * k;
        double SX(double x) => ox + x * k;
        double SY(double y) => oy + y * k;

        // 屏幕框（蓝虚线）——2560×1440
        var frame = new Microsoft.UI.Xaml.Shapes.Rectangle
        {
            Width = sw * k,
            Height = sh * k,
            Stroke = new SolidColorBrush(Microsoft.UI.Colors.SkyBlue),
            StrokeThickness = 2,
            StrokeDashArray = new Microsoft.UI.Xaml.Media.DoubleCollection { 5, 3 },
            Fill = new SolidColorBrush(Microsoft.UI.Colors.Transparent),
        };
        Canvas.SetLeft(frame, SX(0));
        Canvas.SetTop(frame, SY(0));
        cv.Children.Add(frame);

        // 图片：按 contain 烘进 2048×1304 纹理后，再放到纹理矩形里居中
        if (bmp is not null && bmp.PixelWidth > 0)
        {
            double iw = bmp.PixelWidth, ih = bmp.PixelHeight;
            double r = Math.Min(tw / iw, th / ih);
            double dw = iw * r, dh = ih * r;
            var im = new Image
            {
                Source = bmp,
                Width = dw * k,
                Height = dh * k,
                Stretch = Stretch.Uniform,
            };
            Canvas.SetLeft(im, SX(tx + (tw - dw) / 2));
            Canvas.SetTop(im, SY(ty + (th - dh) / 2));
            cv.Children.Add(im);
        }

        // 纹理红框（框住整张 2048×1304，即「6aadf393 这个图」）
        var red = new Microsoft.UI.Xaml.Shapes.Rectangle
        {
            Width = tw * k,
            Height = th * k,
            Stroke = new SolidColorBrush(Windows.UI.Color.FromArgb(255, 255, 92, 92)),
            StrokeThickness = 2,
            Fill = new SolidColorBrush(Microsoft.UI.Colors.Transparent),
        };
        Canvas.SetLeft(red, SX(tx));
        Canvas.SetTop(red, SY(ty));
        cv.Children.Add(red);
    }

    private void LayoutLobby()
    {
        DrawScene(LobbyNowCanvas, _lobbyNowBmp);
        DrawScene(LobbyPickCanvas, _lobbyPickBmp);
    }

    // ----------------------------------------------------------------- 杂项
    private async Task GuardAsync(string what, Func<Task> action)
    {
        try { await action(); }
        catch (Exception ex) { ReportError(what, ex); }
    }

    private void ReportError(string what, Exception ex)
    {
        var msg = $"{what}：{ex.Message}";
        App.AddLog("ERROR", msg);
        SetLobbyStatus(msg);
        SetShopStatus(msg);
    }

    /// <summary>左栏（大厅壁纸）锁定/解锁：只动左栏自己的按钮与提示，不影响右栏。</summary>
    private void SetLobbyBusy(bool busy, string text)
    {
        _busyLobby = busy;
        LobbyApplyBtn.IsEnabled = !busy;
        LobbyRestoreBtn.IsEnabled = !busy && _lobbyReplaced;
        if (LobbyPickBtn is not null) LobbyPickBtn.IsEnabled = !busy;
        if (text.Length > 0) SetLobbyStatus(text);
    }

    /// <summary>右栏（大厅背景）锁定/解锁：只动右栏自己的按钮与提示，不影响左栏。</summary>
    private void SetShopBusy(bool busy, string text)
    {
        _busyShop = busy;
        ShopApplyBtn.IsEnabled = !busy;
        ShopRestoreBtn.IsEnabled = !busy && _shopReplaced;
        if (ShopPickBtn is not null) ShopPickBtn.IsEnabled = !busy;
        if (text.Length > 0) SetShopStatus(text);
    }

    private void SetLobbyStatus(string text) => LobbyStatusText.Text = text;
    private void SetShopStatus(string text) => ShopStatusText.Text = text;

    /// <summary>有没有被替换过。看 any_replaced：六个包里只换了一个也得能点还原。</summary>
    private static bool Flag(JsonElement root, string key)
    {
        if (!root.TryGetProperty(key, out var v) || v.ValueKind != JsonValueKind.Object)
            return false;
        if (v.TryGetProperty("any_replaced", out var any) && any.ValueKind == JsonValueKind.True)
            return true;
        return v.TryGetProperty("replaced", out var r) && r.ValueKind == JsonValueKind.True;
    }

    private static string Str(JsonElement root, string key) =>
        root.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? "" : "";

    private static int Num(JsonElement root, string key) =>
        root.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.Number
            ? v.GetInt32() : 0;

    private static async Task<BitmapImage> LoadBitmapAsync(string path)
    {
        var bytes = await File.ReadAllBytesAsync(path);
        var ms = new MemoryStream(bytes);
        var bmp = new BitmapImage { DecodePixelType = DecodePixelType.Logical };
        await bmp.SetSourceAsync(ms.AsRandomAccessStream());
        return bmp;
    }
}
