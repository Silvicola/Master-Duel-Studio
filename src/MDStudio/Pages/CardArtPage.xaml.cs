using System.Collections.ObjectModel;
using System.Text.Json;
using MDStudio.Models;
using MDStudio.Services;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using Microsoft.UI.Xaml.Navigation;
using Windows.Foundation;
using Windows.Storage.Pickers;

namespace MDStudio.Pages;

/// <summary>
/// 卡图页：给一张卡做超框（over-frame）。
///
/// 单一画法（Floowan OfGradient 风格）：
///   卡图背景填满插图窗 → 主体（透明 PNG）放大跨出框线 → 彩虹整框压顶。
///
/// 预览在本地用 XAML 分层即时绘制（背景图 / 彩虹框 / 主体图层），所以拖鼠标、拉滑块
/// 都立刻跟手：位置只能在卡面上按住拖，大小用滑块（0.5～3×）。点「写入游戏」时再交给
/// Python 按同一套几何合成真正的 704×1024 纹理。
/// </summary>
public sealed partial class CardArtPage : Page
{
    private readonly ObservableCollection<CardItem> _shown = new();
    private CardItem? _current;
    private bool _ready;
    private bool _registered, _ours;
    private string? _artFrame, _artSubject;   // 卡图背景 / 主体（透明 PNG）
    private string _baseArt = "";             // 引擎给的当前卡图（背景兜底）
    private readonly HashSet<int> _officialIds = new();  // 官方超框的卡号
    private readonly HashSet<int> _ourIds = new();       // 本工具做的超框

    // 预览几何（卡片逻辑坐标 704×1024）
    private double _ax = 89, _ay = 191, _aw = 527, _ah = 528, _loreTop = 759;
    private double _cardW = 704, _cardH = 1024, _overflow = 1.38;
    private double _loreX = 30, _loreY = 750, _loreW = 646, _loreH = 224, _loreBottom = 974;
    private double _subW, _subH;              // 主体原图尺寸
    private double _dx, _dy;                  // 主体位移（只用鼠标拖，没有滑块）

    // 边框效果（棱彩 / 虹彩 / 幻彩 / 官方 / 不改边框），互斥单选
    private const string EffPrismatic = "prismatic";    // 棱彩
    private const string EffIridescent = "iridescent";  // 虹彩
    private const string EffGlory = "glory";            // 幻彩（数据键保持 glory，老记录不失效）
    private const string EffOfficial = "official";      // 官方
    private const string EffKeep = "keep";              // 不改边框
    private string _effect = EffPrismatic;
    private string _framePrismaticPath = "";
    private string _frameSolidPath = "";
    private string _borderRainbowPath = "";
    private string _borderGloryPath = "";
    private string _borderOfficialPath = "";
    private BitmapImage? _framePrismaticBmp, _frameSolidBmp;
    private BitmapImage? _borderRainbowBmp, _borderGloryBmp, _borderOfficialBmp;
    private bool _suppressEffectEvent;        // 代码切换选中项时压住 SelectionChanged

    // 拖动状态
    private bool _dragging;
    private Point _dragStart;
    private double _dx0, _dy0;

    public CardArtPage()
    {
        InitializeComponent();
        CardList.ItemsSource = _shown;

        // 边框效果：五种平铺
        EffectBox.Items.Add(MakeEffectItem("棱彩（外沿按卡型换色）", EffPrismatic));
        EffectBox.Items.Add(MakeEffectItem("虹彩（整圈艳彩虹）", EffIridescent));
        EffectBox.Items.Add(MakeEffectItem("幻彩（柔和淡彩环带）", EffGlory));
        EffectBox.Items.Add(MakeEffectItem("官方（还原官方超框）", EffOfficial));
        EffectBox.Items.Add(MakeEffectItem("不改边框（保留原生卡框）", EffKeep));

        // RD 面板：边框效果（与超框同一套素材），默认「不改边框」= RD 原框
        RdEffectBox.Items.Add(MakeEffectItem("不改边框（保留 RD 原框）", EffKeep));
        RdEffectBox.Items.Add(MakeEffectItem("虹彩（整圈艳彩虹）", EffIridescent));
        RdEffectBox.Items.Add(MakeEffectItem("幻彩（柔和淡彩环带）", EffGlory));
        RdEffectBox.Items.Add(MakeEffectItem("官方（还原官方超框）", EffOfficial));
        SelectRdEffect(EffKeep);
        RdAlphaSlider.Value = 1;

        _ready = true;
        _ = LoadFramesAsync();
    }

    protected override async void OnNavigatedTo(NavigationEventArgs e)
    {
        base.OnNavigatedTo(e);
        if (App.Engine is null)
        {
            SetStatus("引擎没有启动：" + (App.EngineError ?? "未知原因"));
            return;
        }
        if (_shown.Count == 0) await SearchAsync("");
    }

    // ------------------------------------------------------------- 初始化
    private bool _framesLoaded;

    private async Task LoadFramesAsync()
    {
        if (_framesLoaded || App.Engine is null) return;
        try
        {
            await RefreshGateAsync();
            _framesLoaded = true;
        }
        catch (Exception ex)
        {
            SetStatus("读超框登记表失败：" + ex.Message);
        }
    }

    private async Task RefreshGateAsync()
    {
        if (App.Engine is not { } engine) return;
        var gate = await engine.CallAsync("overframe.gate");
        _officialIds.Clear();
        _ourIds.Clear();
        if (gate.ValueKind == JsonValueKind.Object)
        {
            ReadIds(gate, "official", _officialIds);
            ReadIds(gate, "ours", _ourIds);
        }
        Retag();
    }

    private static void ReadIds(JsonElement root, string key, HashSet<int> into)
    {
        if (!root.TryGetProperty(key, out var arr) || arr.ValueKind != JsonValueKind.Array) return;
        foreach (var c in arr.EnumerateArray())
            if (c.TryGetInt32(out var cid)) into.Add(cid);
    }

    /// <summary>把标签重新贴到列表上。写盘/撤回之后调用，标签要立刻跟着变。</summary>
    private void Retag()
    {
        foreach (var c in _shown)
        {
            c.OfficialArt = _officialIds.Contains(c.Id);
            c.OurArt = _ourIds.Contains(c.Id);
        }
    }

    // --------------------------------------------------------------- 搜索
    private CancellationTokenSource? _searchCts;
    private const int SearchLimit = 200;

    private void SearchBox_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (_ready) _ = SearchAsync(SearchBox.Text.Trim());
    }

    private async Task SearchAsync(string q)
    {
        if (App.Engine is not { } engine) return;

        _searchCts?.Cancel();
        var cts = new CancellationTokenSource();
        _searchCts = cts;
        try
        {
            if (q.Length > 0) await Task.Delay(250, cts.Token);
        }
        catch (OperationCanceledException)
        {
            return;
        }

        SetBusy(true, null);
        try
        {
            var data = await engine.CallAsync("cards.search", new Dictionary<string, object?>
            {
                ["q"] = q,
                ["limit"] = SearchLimit,
                ["has_anim"] = false,
            });
            if (cts.IsCancellationRequested) return;

            _shown.Clear();
            foreach (var c in ParseCards(data))
            {
                c.OfficialArt = _officialIds.Contains(c.Id);
                c.OurArt = _ourIds.Contains(c.Id);
                _shown.Add(c);
            }
            EmptyHint.Visibility = _shown.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
            // 计数放在左栏里（不再占底部状态条），两页空闲时底部都折叠，面板高度才对得上
            StatusStrip.Visibility = Visibility.Collapsed;
            CountText.Text = q.Length == 0
                ? $"共 {_shown.Count} 张（最多列 {SearchLimit} 张）"
                : $"「{q}」找到 {_shown.Count} 张";
        }
        catch (Exception ex)
        {
            SetStatus("搜索失败：" + ex.Message);
        }
        finally
        {
            SetBusy(false, null);
        }
    }

    private static List<CardItem> ParseCards(JsonElement data)
    {
        var list = new List<CardItem>();
        if (data.ValueKind != JsonValueKind.Object ||
            !data.TryGetProperty("cards", out var arr) || arr.ValueKind != JsonValueKind.Array)
            return list;

        foreach (var c in arr.EnumerateArray())
        {
            list.Add(new CardItem
            {
                Id = c.TryGetProperty("id", out var id) ? id.GetInt32() : 0,
                Name = c.TryGetProperty("name", out var n) && n.ValueKind == JsonValueKind.String
                    ? n.GetString() ?? "" : "",
                Real = c.TryGetProperty("real", out var r) ? r.GetInt32() : 0,
                Unreleased = c.TryGetProperty("unreleased", out var u) && u.ValueKind == JsonValueKind.True,
            });
        }
        return list;
    }

    // --------------------------------------------------------------- 选中
    /// <summary>
    /// 占位副本号灰掉、点不动。
    ///
    /// 游戏内部有一批跟真卡同名同图的副本号，渲染时不走它，写上去也不会有反应。
    /// 判据在后端（card_rarity_asset 里有没有这个号），异画两个号都在表里，不受影响。
    /// </summary>
    private void CardList_ContainerContentChanging(ListViewBase sender, ContainerContentChangingEventArgs args)
    {
        if (args.ItemContainer is not ListViewItem box || args.Item is not CardItem card) return;
        // 容器会被回收复用，两个方向都要显式设回去
        box.IsEnabled = !card.IsPlaceholder;
        box.Opacity = card.IsPlaceholder ? 0.45 : 1.0;
    }

    private async void CardList_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (CardList.SelectedItem is not CardItem item)
        {
            _current = null;
            CardName.Text = "选一张卡";
            CardMeta.Text = "";
            return;
        }
        if (item.IsPlaceholder)
        {
            // 灰掉之后本来就点不到，兜一层防止别处把它选中
            CardList.SelectedItem = null;
            SetStatus($"{item.Title}：#{item.Id} 是占位号，游戏实际渲染 #{item.Real}");
            return;
        }
        _current = item;
        CardName.Text = item.Title;
        CardMeta.Text = $"#{item.Id}";
        ResetArt();
        RdResetForCard();
        await LoadCurrentAsync(item);
        await RdRestoreEditAsync(item.Id);
    }

    private void ResetArt()
    {
        _artFrame = _artSubject = null;
        _subW = _subH = 0;
        FrameArtBox.Text = "";
        SubjectBox.Text = "";
        ArtBrush.ImageSource = null;
        SubjectImg.Source = null;
        SubjectImg.Visibility = Visibility.Collapsed;
    }

    /// <summary>选中卡之后：读卡图状况 + 预览几何，然后本地画出超框预览。</summary>
    private async Task LoadCurrentAsync(CardItem item)
    {
        if (App.Engine is not { } engine) return;
        ApplyBtn.IsEnabled = false;
        RevertBtn.IsEnabled = false;
        PreviewHint.Text = "正在读卡图…";
        PreviewHint.Visibility = Visibility.Visible;

        try
        {
            var data = await engine.CallAsync("overframe.status", new Dictionary<string, object?>
            {
                ["card"] = item.Id,
            });

            var exists = data.ValueKind == JsonValueKind.Object &&
                         data.TryGetProperty("exists", out var ex) && ex.GetBoolean();
            if (!exists)
            {
                var why = data.ValueKind == JsonValueKind.Object &&
                          data.TryGetProperty("error", out var er) ? er.GetString() ?? "" : "";
                PreviewHint.Text = string.IsNullOrEmpty(why) ? "这张卡没有卡图" : why;
                return;
            }

            _registered = data.TryGetProperty("registered", out var rg) && rg.GetBoolean();
            _ours = data.TryGetProperty("ours", out var ou) && ou.GetBoolean();
            var w = data.TryGetProperty("width", out var wv) ? wv.GetInt32() : 0;
            var h = data.TryGetProperty("height", out var hv) ? hv.GetInt32() : 0;
            PreviewCaption.Text = $"{w} × {h}";

            await SetupPreviewAsync(item.Id);

            RevertBtn.IsEnabled = _ours;
            ApplyBtn.IsEnabled = true;
        }
        catch (Exception ex)
        {
            PreviewHint.Text = "读不了这张卡：" + ex.Message;
        }
    }

    /// <summary>拉预览几何 + 卡框图 + 底色卡图，搭好本地分层面板。</summary>
    private async Task SetupPreviewAsync(int card)
    {
        if (App.Engine is not { } engine) return;
        var lay = await engine.CallAsync("overframe.layout", new Dictionary<string, object?>
        {
            ["card"] = card,
        });
        if (lay.ValueKind != JsonValueKind.Object)
        {
            PreviewHint.Text = "读不出预览信息";
            return;
        }

        _ax = lay.TryGetProperty("ax", out var v1) ? v1.GetDouble() : 89;
        _ay = lay.TryGetProperty("ay", out var v2) ? v2.GetDouble() : 191;
        _aw = lay.TryGetProperty("aw", out var v3) ? v3.GetDouble() : 527;
        _ah = lay.TryGetProperty("ah", out var v4) ? v4.GetDouble() : 528;
        _loreTop = lay.TryGetProperty("lore_top", out var v5) ? v5.GetDouble() : 759;
        _cardW = lay.TryGetProperty("card_w", out var v6) ? v6.GetDouble() : 704;
        _cardH = lay.TryGetProperty("card_h", out var v7) ? v7.GetDouble() : 1024;
        _overflow = lay.TryGetProperty("overflow_scale", out var v8) ? v8.GetDouble() : 1.38;
        _baseArt = lay.TryGetProperty("base_art", out var v9) ? v9.GetString() ?? "" : "";
        _loreX = lay.TryGetProperty("lore_x", out var lx) ? lx.GetDouble() : 30;
        _loreY = lay.TryGetProperty("lore_y", out var ly) ? ly.GetDouble() : 750;
        _loreW = lay.TryGetProperty("lore_w", out var lw) ? lw.GetDouble() : 646;
        _loreH = lay.TryGetProperty("lore_h", out var lh) ? lh.GetDouble() : 224;
        _loreBottom = lay.TryGetProperty("lore_bottom", out var lb) ? lb.GetDouble() : 974;

        // 插图窗（幻彩是左右满幅，具体几何在 ApplyArtRect 里按效果取）

        // 主体图层只裁到卡片下沿（可从效果框下穿过，在卡底显示）
        SubjectLayer.Clip = new RectangleGeometry { Rect = new Rect(0, 0, _cardW, _cardH) };

        // 上次应用过就自动加载素材与参数，方便在原有基础上继续调
        var hasEdit = lay.TryGetProperty("has_edit", out var he) && he.GetBoolean();
        if (hasEdit)
        {
            _artFrame = _baseArt.Length > 0 ? _baseArt : null;
            var aname = lay.TryGetProperty("edit_art_name", out var an) ? an.GetString() : null;
            FrameArtBox.Text = !string.IsNullOrEmpty(aname)
                ? aname
                : (_baseArt.Length > 0 ? Path.GetFileName(_baseArt) : "");

            var subj = lay.TryGetProperty("edit_subject", out var sj) ? sj.GetString() : null;
            if (!string.IsNullOrEmpty(subj) && File.Exists(subj))
            {
                _artSubject = subj;
                var sbmp = await LoadFileBitmapAsync(subj);
                _subW = sbmp.PixelWidth;
                _subH = sbmp.PixelHeight;
                SubjectImg.Source = sbmp;
                var sname = lay.TryGetProperty("edit_subject_name", out var sn) ? sn.GetString() : null;
                SubjectBox.Text = string.IsNullOrEmpty(sname) ? Path.GetFileName(subj) : sname;
            }

            SubjectScaleSlider.Value = lay.TryGetProperty("edit_scale", out var es) ? es.GetDouble() : 1.0;
            _dx = lay.TryGetProperty("edit_dx", out var edx) ? edx.GetDouble() : 0;
            _dy = lay.TryGetProperty("edit_dy", out var edy) ? edy.GetDouble() : 0;
            LoreAlphaSlider.Value = lay.TryGetProperty("edit_lore_alpha", out var ela) ? ela.GetDouble() : 1.0;
        }
        else
        {
            _artFrame = null;
            _artSubject = null;
            _subW = _subH = 0;
            SubjectImg.Source = null;
            FrameArtBox.Text = "";
            SubjectBox.Text = "";
            SubjectScaleSlider.Value = 1;
            _dx = _dy = 0;
            LoreAlphaSlider.Value = 1;
        }

        // 卡框图（棱彩 / 原生两套）与三张外沿素材路径：来自本次 layout，预加载好，
        // 切换效果时直接换，不用再等一次引擎往返
        _framePrismaticPath = lay.TryGetProperty("frame_path_prismatic", out var fp) ? fp.GetString() ?? "" : "";
        _frameSolidPath = lay.TryGetProperty("frame_path_solid", out var fs) ? fs.GetString() ?? "" : "";
        _borderRainbowPath = lay.TryGetProperty("border_rainbow_path", out var br) ? br.GetString() ?? "" : "";
        _borderGloryPath = lay.TryGetProperty("border_glory_path", out var bg) ? bg.GetString() ?? "" : "";
        _borderOfficialPath = lay.TryGetProperty("border_official_path", out var bo) ? bo.GetString() ?? "" : "";
        _framePrismaticBmp = await TryLoad(_framePrismaticPath);
        _frameSolidBmp = await TryLoad(_frameSolidPath);
        if (_borderRainbowBmp is null)
            _borderRainbowBmp = await TryLoad(_borderRainbowPath);
        if (_borderGloryBmp is null)
            _borderGloryBmp = await TryLoad(_borderGloryPath);
        if (_borderOfficialBmp is null)
            _borderOfficialBmp = await TryLoad(_borderOfficialPath);

        // 选效果：之前应用过就还原当时的效果，否则默认棱彩
        var eff = hasEdit
            ? (lay.TryGetProperty("edit_effect", out var ee) ? ee.GetString() ?? EffPrismatic : EffPrismatic)
            : EffPrismatic;
        SelectEffect(eff);

        // 效果框图层裁到框矩形、透明度按滑块
        LoreImg.Clip = new RectangleGeometry { Rect = new Rect(_loreX, _loreY, _loreW, _loreH) };

        // 连接怪兽：两张箭头层由引擎现成给好，这里直接叠上去。
        // 未点亮那层跟效果框同一个透明度，滑块一动就跟着淡出。
        LinkLitImg.Source = null;
        LinkDimImg.Source = null;
        if (lay.TryGetProperty("link", out var lk))
        {
            var dimPath = lk.TryGetProperty("dim", out var dp) ? dp.GetString() ?? "" : "";
            var litPath = lk.TryGetProperty("lit", out var lp2) ? lp2.GetString() ?? "" : "";
            if (litPath.Length > 0 && File.Exists(litPath))
                LinkLitImg.Source = await LoadFileBitmapAsync(litPath);
            if (dimPath.Length > 0 && File.Exists(dimPath))
                LinkDimImg.Source = await LoadFileBitmapAsync(dimPath);
        }
        LoreImg.Opacity = LoreAlphaSlider.Value;

        // 背景：用户选的优先，否则用引擎给的当前卡图
        var art = _artFrame ?? (_baseArt.Length > 0 && File.Exists(_baseArt) ? _baseArt : null);
        if (art is not null)
        {
            ArtBrush.ImageSource = await LoadFileBitmapAsync(art);
            PreviewHint.Visibility = Visibility.Collapsed;
        }
        else
        {
            ArtBrush.ImageSource = null;
            PreviewHint.Text = "选一张卡图背景就能预览（这张卡的插图不在本地）";
            PreviewHint.Visibility = Visibility.Visible;
        }

        UpdateSubjectLayout();
    }

    /// <summary>按当前大小与位移摆放主体（本地即时，不碰引擎）。</summary>
    private void UpdateSubjectLayout()
    {
        if (_subW <= 0 || _subH <= 0)
        {
            SubjectImg.Visibility = Visibility.Collapsed;
            return;
        }
        var cover = Math.Max(_aw / _subW, _ah / _subH);
        var r = cover * _overflow * Math.Max(0.1, SubjectScaleSlider.Value);
        var sw = _subW * r;
        var sh = _subH * r;
        var sx = _ax + (_aw - sw) / 2 + _dx;
        var sy = _ay + (_ah - sh) / 2 + _dy;
        SubjectImg.Width = sw;
        SubjectImg.Height = sh;
        Canvas.SetLeft(SubjectImg, sx);
        Canvas.SetTop(SubjectImg, sy);
        SubjectImg.Visibility = Visibility.Visible;
    }

    // ------------------------------------------------------------- 选图
    private async void PickFrameArt_Click(object sender, RoutedEventArgs e)
    {
        var f = await PickImageAsync();
        if (f is null) return;
        _artFrame = f;
        FrameArtBox.Text = Path.GetFileName(f);
        ArtBrush.ImageSource = await LoadFileBitmapAsync(f);
        PreviewHint.Visibility = Visibility.Collapsed;
    }

    private async void PickSubject_Click(object sender, RoutedEventArgs e)
    {
        var f = await PickImageAsync();
        if (f is null) return;
        _artSubject = f;
        SubjectBox.Text = Path.GetFileName(f);
        var bmp = await LoadFileBitmapAsync(f);
        _subW = bmp.PixelWidth;
        _subH = bmp.PixelHeight;
        SubjectImg.Source = bmp;
        UpdateSubjectLayout();
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

    // ------------------------------------------------------------- 拖动主体
    private void CardCanvas_PointerPressed(object sender, PointerRoutedEventArgs e)
    {
        if (_subW <= 0) return;                 // 没有主体就没什么可拖
        _dragging = true;
        _dragStart = e.GetCurrentPoint(CardCanvas).Position;
        _dx0 = _dx;
        _dy0 = _dy;
        CardCanvas.CapturePointer(e.Pointer);
        e.Handled = true;
    }

    private void CardCanvas_PointerMoved(object sender, PointerRoutedEventArgs e)
    {
        if (!_dragging) return;
        var p = e.GetCurrentPoint(CardCanvas).Position;
        // 位置只靠鼠标拖，没有滑块了；给个卡面大小的活动范围，别把主体拖飞
        _dx = Clamp(_dx0 + (p.X - _dragStart.X), -_cardW, _cardW);
        _dy = Clamp(_dy0 + (p.Y - _dragStart.Y), -_cardH, _cardH);
        UpdateSubjectLayout();
        e.Handled = true;
    }

    private void CardCanvas_PointerReleased(object sender, PointerRoutedEventArgs e)
    {
        if (!_dragging) return;
        _dragging = false;
        CardCanvas.ReleasePointerCapture(e.Pointer);
        e.Handled = true;
    }

    private void CardCanvas_PointerCaptureLost(object sender, PointerRoutedEventArgs e)
    {
        _dragging = false;
    }

    private static double Clamp(double v, double lo, double hi) =>
        v < lo ? lo : (v > hi ? hi : v);

    // ------------------------------------------------------------- 参数
    private void Tune_ValueChanged(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (!_ready) return;          // 构造期间 Value 也会触发一次，那时读数控件还没建出来
        ScaleText.Text = $"{SubjectScaleSlider.Value:0.0}×";
        UpdateSubjectLayout();
    }

    private void LoreAlpha_ValueChanged(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (!_ready) return;
        var a = Alpha;
        LoreText.Text = $"{a * 100:0}%";
        LoreImg.Opacity = a;
    }

    /// <summary>效果框透明度，按步长 0.01 取整，别让 0.01 累加出来的浮点毛刺漏到界面和写盘参数上。</summary>
    private double Alpha => Math.Round(LoreAlphaSlider.Value, 2);

    private new double Scale => Math.Round(SubjectScaleSlider.Value, 1);

    // ------------------------------------------------------------- 边框效果
    private static ComboBoxItem MakeEffectItem(string label, string effect)
    {
        return new ComboBoxItem { Content = label, Tag = effect };
    }

    /// <summary>下拉框选了哪种效果。</summary>
    private void EffectBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_suppressEffectEvent || !_ready) return;
        if (EffectBox.SelectedItem is ComboBoxItem it && it.Tag is string eff)
        {
            _effect = eff;
            RefreshEffectVisual();
        }
    }

    /// <summary>代码侧切换效果（还原已有编辑 / 默认），压住 SelectionChanged 避免回环。</summary>
    private void SelectEffect(string effect)
    {
        _effect = effect;
        _suppressEffectEvent = true;
        try
        {
            foreach (ComboBoxItem? it in EffectBox.Items)
            {
                if (it?.Tag is string tag && tag == effect)
                {
                    if (!Equals(EffectBox.SelectedItem, it)) EffectBox.SelectedItem = it;
                    break;
                }
            }
        }
        finally
        {
            _suppressEffectEvent = false;
        }
        RefreshEffectVisual();
    }

    /// <summary>插图区几何：一律只填插图窗，不跨到边框。</summary>
    private void ApplyArtRect()
    {
        ArtRect.Width = _aw;
        ArtRect.Height = _ah;
        Canvas.SetLeft(ArtRect, _ax);
        Canvas.SetTop(ArtRect, _ay);
    }

    /// <summary>按当前效果刷新卡框与外沿叠加层。
    /// 棱彩换用按卡型配色的卡框；虹彩/幻彩/官方用原生框 + 对应的外沿素材。</summary>
    private void RefreshEffectVisual()
    {
        var fb = _effect == EffPrismatic ? _framePrismaticBmp : _frameSolidBmp;
        if (fb is not null)
        {
            FrameImg.Source = fb;
            LoreImg.Source = fb;
        }
        ApplyArtRect();

        BorderRainbowImg.Visibility = Visibility.Collapsed;
        BorderGloryImg.Visibility = Visibility.Collapsed;
        BorderOfficialImg.Visibility = Visibility.Collapsed;

        if (_effect == EffIridescent && _borderRainbowBmp is not null)
        {
            BorderRainbowImg.Source = _borderRainbowBmp;
            BorderRainbowImg.Visibility = Visibility.Visible;
        }
        else if (_effect == EffGlory && _borderGloryBmp is not null)
        {
            BorderGloryImg.Source = _borderGloryBmp;
            BorderGloryImg.Visibility = Visibility.Visible;
        }
        else if (_effect == EffOfficial && _borderOfficialBmp is not null)
        {
            BorderOfficialImg.Source = _borderOfficialBmp;
            BorderOfficialImg.Visibility = Visibility.Visible;
        }
    }

    private static async Task<BitmapImage?> TryLoad(string path)
    {
        if (string.IsNullOrEmpty(path) || !File.Exists(path)) return null;
        try
        {
            return await LoadFileBitmapAsync(path);
        }
        catch
        {
            return null;
        }
    }

    private static async Task<BitmapImage> LoadFileBitmapAsync(string path)
    {
        var bytes = await File.ReadAllBytesAsync(path);
        var ms = new MemoryStream(bytes);
        var bmp = new BitmapImage { DecodePixelType = DecodePixelType.Logical };
        await bmp.SetSourceAsync(ms.AsRandomAccessStream());
        return bmp;
    }

    // ------------------------------------------------------------- 写入
    private async void ApplyBtn_Click(object sender, RoutedEventArgs e)
    {
        if (_current is null || App.Engine is not { } engine) return;

        var hasArt = _artFrame is not null || (_baseArt.Length > 0 && File.Exists(_baseArt));
        if (!hasArt)
        {
            SetStatus("先选一张卡图背景（这张卡的插图不在本地）");
            return;
        }

        ApplyBtn.IsEnabled = false;
        SetBusy(true, App.DryRun ? "正在写卡图（预演）…" : "正在写卡图…");
        SetLocked(true);
        try
        {
            await engine.CallAsync("overframe.apply", new Dictionary<string, object?>
            {
                ["card"] = _current.Id,
                ["art"] = _artFrame,
                ["subject"] = _artSubject,
                ["scale"] = Scale,
                ["dx"] = (int)_dx,
                ["dy"] = (int)_dy,
                ["lore_alpha"] = Alpha,
                ["effect"] = _effect,
                ["dry_run"] = App.DryRun,
            });

            SetStatus(App.DryRun
                ? $"{_current.Title}：预演结束，没有写游戏文件"
                : $"{_current.Title}：超框已写入（完全退出游戏再重开才会加载）");
            if (!App.DryRun)
            {
                _ours = true;
                _registered = true;
                RevertBtn.IsEnabled = true;
                await RefreshGateAsync();
                await LoadCurrentAsync(_current);
            }
        }
        catch (EngineException ex) when (ex.Kind == "Canceled")
        {
            SetStatus("已取消");
        }
        catch (Exception ex)
        {
            SetStatus("写入失败：" + ex.Message);
            App.AddLog("ERROR", $"{_current.Title} 超框写入失败：{ex.Message}");
            ApplyBtn.IsEnabled = true;
        }
        finally
        {
            SetLocked(false);
            SetBusy(false, null);
        }
    }

    private async void RevertBtn_Click(object sender, RoutedEventArgs e)
    {
        if (_current is null || App.Engine is not { } engine) return;

        var dlg = new ContentDialog
        {
            Title = "撤回这张卡的超框",
            Content = $"{_current.Title} 会回到原卡图，超框登记也一并撤掉。",
            PrimaryButtonText = "撤回",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Close,
            XamlRoot = XamlRoot,
        };
        if (await dlg.ShowAsync() != ContentDialogResult.Primary) return;

        SetBusy(true, App.DryRun ? "正在撤回（预演）…" : "正在撤回…");
        SetLocked(true);
        try
        {
            await engine.CallAsync("overframe.remove", new Dictionary<string, object?>
            {
                ["card"] = _current.Id,
                ["dry_run"] = App.DryRun,
            });
            SetStatus(App.DryRun
                ? "预演结束，没有写游戏文件"
                : $"{_current.Title}：已回到原卡图");
            if (!App.DryRun)
            {
                _ours = false;
                RevertBtn.IsEnabled = false;
                await RefreshGateAsync();
                await LoadCurrentAsync(_current);
            }
        }
        catch (EngineException ex)
        {
            SetStatus("撤回失败：" + ex.Message);
        }
        finally
        {
            SetLocked(false);
            SetBusy(false, null);
        }
    }

    // --------------------------------------------------------------- 状态
    private void SetStatus(string text)
    {
        StatusStrip.Visibility = Visibility.Visible;
        StatusText.Text = text;
    }

    /// <summary>整页上锁：写入 / 撤回期间不让改参数、不让换卡。左侧导航还能切到别的页。</summary>
    private void SetLocked(bool locked)
    {
        BusyOverlay.Visibility = locked ? Visibility.Visible : Visibility.Collapsed;
    }

    private void SetBusy(bool busy, string? text)
    {
        CardList.IsEnabled = !busy;
        Progress.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
        Progress.IsIndeterminate = busy;
        if (text is not null) SetStatus(text);
    }

    // ================================================================ Rush Duel 工作台
    // RD 版式是整卡自绘：卡名/攻防/效果文字都画进贴图（超框登记后游戏原生文字层被
    // 抑制），所以预览直接显示引擎合成的 PNG；改参数就重新合成一张。
    private string? _rdArt, _rdSubject;
    private double _rdDx, _rdDy;
    private string _rdEffect = EffKeep;      // 边框效果，默认 RD 原框
    private double _rdAlpha = 1.0;           // 框体不透明度
    private double _rdSubX, _rdSubY;          // 主体在卡面上的基准位置（引擎给出）
    private double _rdSubW, _rdSubH;
    private bool _rdDragging;
    private Point _rdDragStart;
    private double _rdDx0, _rdDy0;
    private CancellationTokenSource? _rdCts;
    private string _rdBaseStamp = "", _rdInfoStamp = "", _rdSubStamp = "", _rdPanelStamp = "";

    private double RdScale => Math.Round(RdScaleSlider.Value, 1);

    /// <summary>换卡时清空 RD 面板的选图与状态（参数由编辑存档还原）。</summary>
    private void RdResetForCard()
    {
        _rdArt = _rdSubject = null;
        _rdDx = _rdDy = 0;
        _rdBaseStamp = _rdInfoStamp = _rdSubStamp = _rdPanelStamp = "";
        RdArtBox.Text = "";
        RdSubjectBox.Text = "";
        RdScaleSlider.Value = 1;
        _rdEffect = EffKeep;
        if (RdEffectBox is not null) SelectRdEffect(EffKeep);
        _rdAlpha = 1.0;
        if (RdAlphaSlider is not null) RdAlphaSlider.Value = 1;
        RdBaseImg.Source = null;
        RdInfoImg.Source = null;
        RdPanelImg.Source = null;
        RdPanelImg.Opacity = 1;
        RdSubjImg.Source = null;
        RdSubjImg.Visibility = Visibility.Collapsed;
        RdCaption.Text = "";
        RdHint.Text = "选一张卡";
        RdHint.Visibility = _current is null ? Visibility.Visible : Visibility.Collapsed;
    }

    /// <summary>还原这张卡上次写入的素材与参数，然后出预览。</summary>
    private async Task RdRestoreEditAsync(int card)
    {
        if (App.Engine is not { } engine) return;
        RdHint.Visibility = Visibility.Collapsed;
        try
        {
            var ed = await engine.CallAsync("rushduel.edit", new Dictionary<string, object?>
            {
                ["card"] = card,
            });
            if (ed.ValueKind == JsonValueKind.Object)
            {
                if (ed.TryGetProperty("art", out var ra) && ra.ValueKind == JsonValueKind.String)
                {
                    var p = ra.GetString() ?? "";
                    if (File.Exists(p))
                    {
                        _rdArt = p;
                        var nm = ed.TryGetProperty("art_name", out var an) ? an.GetString() : null;
                        RdArtBox.Text = string.IsNullOrEmpty(nm) ? Path.GetFileName(p) : nm;
                    }
                }
                if (ed.TryGetProperty("subject", out var rs) && rs.ValueKind == JsonValueKind.String)
                {
                    var p = rs.GetString() ?? "";
                    if (File.Exists(p))
                    {
                        _rdSubject = p;
                        var nm = ed.TryGetProperty("subject_name", out var sn) ? sn.GetString() : null;
                        RdSubjectBox.Text = string.IsNullOrEmpty(nm) ? Path.GetFileName(p) : nm;
                    }
                }
                if (ed.TryGetProperty("scale", out var rc) && rc.ValueKind == JsonValueKind.Number)
                    RdScaleSlider.Value = rc.GetDouble();
                _rdDx = ed.TryGetProperty("dx", out var rx) ? rx.GetDouble() : 0;
                _rdDy = ed.TryGetProperty("dy", out var ry) ? ry.GetDouble() : 0;
                if (ed.TryGetProperty("effect", out var re) && re.ValueKind == JsonValueKind.String)
                    SelectRdEffect(re.GetString() ?? EffKeep);
                JsonElement ra2;
                if ((ed.TryGetProperty("frame_alpha", out ra2) || ed.TryGetProperty("info_alpha", out ra2))
                    && ra2.ValueKind == JsonValueKind.Number)
                {
                    _rdAlpha = Math.Round(ra2.GetDouble(), 2);
                    RdAlphaSlider.Value = _rdAlpha;
                    if (RdAlphaText is not null) RdAlphaText.Text = $"{_rdAlpha * 100:0}%";
                }
            }
        }
        catch
        {
            // 没有存档就按默认参数来
        }
        await RdRefreshAsync();
    }

    /// <summary>拉三层（底图 / 主体 / 信息层）叠起来显示。参数变动才拉，拖动主体只本地平移。</summary>
    private async Task RdRefreshAsync(int debounceMs = 80)
    {
        if (App.Engine is not { } engine || _current is null) return;
        _rdCts?.Cancel();
        var cts = new CancellationTokenSource();
        _rdCts = cts;
        RdHint.Visibility = Visibility.Collapsed;
        RdApplyBtn.IsEnabled = false;
        try
        {
            if (debounceMs > 0)
            {
                try { await Task.Delay(debounceMs, cts.Token); }
                catch (OperationCanceledException) { return; }
            }

            var data = await engine.CallAsync("rushduel.layers", new Dictionary<string, object?>
            {
                ["card"] = _current.Id,
                ["art"] = _rdArt,
                ["subject"] = _rdSubject,
                ["scale"] = RdScale,
                ["effect"] = _rdEffect,
                ["frame_alpha"] = _rdAlpha,
            });
            if (cts.IsCancellationRequested) return;

            if (data.ValueKind == JsonValueKind.Object)
            {
                var baseP = data.TryGetProperty("base", out var bp) ? bp.GetString() ?? "" : "";
                var infoP = data.TryGetProperty("info", out var ip) ? ip.GetString() ?? "" : "";
                var subP = data.TryGetProperty("subject", out var sp) ? sp.GetString() ?? "" : "";
                var baseStamp = data.TryGetProperty("base_stamp", out var bs) ? bs.GetString() ?? "" : "";
                var infoStamp = data.TryGetProperty("info_stamp", out var ist) ? ist.GetString() ?? "" : "";
                var subStamp = data.TryGetProperty("sub_stamp", out var sst) ? sst.GetString() ?? "" : "";
                // 图层内容没变就不重读（引擎会复用上次的图），省掉解码与闪烁
                if (baseP.Length > 0 && File.Exists(baseP)
                    && (RdBaseImg.Source is null || baseStamp != _rdBaseStamp))
                {
                    RdBaseImg.Source = await LoadFileBitmapAsync(baseP);
                    _rdBaseStamp = baseStamp;
                }
                if (infoP.Length > 0 && File.Exists(infoP)
                    && (RdInfoImg.Source is null || infoStamp != _rdInfoStamp))
                {
                    RdInfoImg.Source = await LoadFileBitmapAsync(infoP);
                    _rdInfoStamp = infoStamp;
                }
                if (subP.Length > 0 && File.Exists(subP))
                {
                    if (RdSubjImg.Source is null || subStamp != _rdSubStamp)
                    {
                        RdSubjImg.Source = await LoadFileBitmapAsync(subP);
                        _rdSubStamp = subStamp;
                    }
                    _rdSubW = data.TryGetProperty("sub_w", out var sw) ? sw.GetDouble() : 0;
                    _rdSubH = data.TryGetProperty("sub_h", out var sh) ? sh.GetDouble() : 0;
                    _rdSubX = data.TryGetProperty("sub_x", out var sx) ? sx.GetDouble() : 0;
                    _rdSubY = data.TryGetProperty("sub_y", out var sy) ? sy.GetDouble() : 0;
                    RdSubjImg.Width = _rdSubW;
                    RdSubjImg.Height = _rdSubH;
                    RdSubjImg.Visibility = Visibility.Visible;
                    PlaceSubject();
                }
                else
                {
                    RdSubjImg.Source = null;
                    RdSubjImg.Visibility = Visibility.Collapsed;
                }
                // 效果框单独一层，压主体上面；不透明度只改这一层的 Opacity，不重合成
                var panelP = data.TryGetProperty("panel", out var pp2) ? pp2.GetString() ?? "" : "";
                var panelStamp = data.TryGetProperty("panel_stamp", out var pst) ? pst.GetString() ?? "" : "";
                if (panelP.Length > 0 && File.Exists(panelP))
                {
                    if (RdPanelImg.Source is null || panelStamp != _rdPanelStamp)
                    {
                        RdPanelImg.Source = await LoadFileBitmapAsync(panelP);
                        _rdPanelStamp = panelStamp;
                    }
                    RdPanelImg.Visibility = Visibility.Visible;
                }
                else
                {
                    RdPanelImg.Source = null;
                    RdPanelImg.Visibility = Visibility.Collapsed;
                }
                RdPanelImg.Opacity = _rdAlpha;
                var nm = data.TryGetProperty("name", out var n2) ? n2.GetString() ?? "" : "";
                var atk = data.TryGetProperty("atk", out var a2) && a2.ValueKind == JsonValueKind.Number
                    ? a2.GetInt32().ToString() : "—";
                var dfn = data.TryGetProperty("def", out var d2) && d2.ValueKind == JsonValueKind.Number
                    ? d2.GetInt32().ToString() : "—";
                RdCaption.Text = $"{nm}　ATK {atk} / DEF {dfn}";
            }
            RdApplyBtn.IsEnabled = true;
            RdRevertBtn.IsEnabled = _ours;
        }
        catch (OperationCanceledException)
        {
        }
        catch (Exception ex)
        {
            RdHint.Text = "预览失败：" + ex.Message;
            RdHint.Visibility = Visibility.Visible;
        }
    }

    /// <summary>按当前累计位移把主体图层摆到卡面上（纯本地，拖动零延迟）。</summary>
    private void PlaceSubject()
    {
        if (RdSubjImg is null) return;
        Canvas.SetLeft(RdSubjImg, _rdSubX + _rdDx);
        Canvas.SetTop(RdSubjImg, _rdSubY + _rdDy);
    }

    private void SectionBar_SelectionChanged(SelectorBar sender, SelectorBarSelectionChangedEventArgs args)
    {
        if (!_ready) return;
        bool rd = SectionBar.SelectedItem is SelectorBarItem it && (it.Tag as string) == "rushduel";
        OverframeSection.Visibility = rd ? Visibility.Collapsed : Visibility.Visible;
        RdPanel.Visibility = rd ? Visibility.Visible : Visibility.Collapsed;
        if (rd && _current is not null) _ = RdRefreshAsync();
    }

    private async void RdPickArt_Click(object sender, RoutedEventArgs e)
    {
        var f = await PickImageAsync();
        if (f is null) return;
        _rdArt = f;
        RdArtBox.Text = Path.GetFileName(f);
        await RdRefreshAsync();
    }

    private async void RdPickSubject_Click(object sender, RoutedEventArgs e)
    {
        var f = await PickImageAsync();
        if (f is null) return;
        _rdSubject = f;
        RdSubjectBox.Text = Path.GetFileName(f);
        await RdRefreshAsync();
    }

    private void RdScale_ValueChanged(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (!_ready) return;
        RdScaleText.Text = $"{RdScale:0.0}×";
        _ = RdRefreshAsync();
    }

    private void RdEffectBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!_ready) return;
        if (RdEffectBox.SelectedItem is ComboBoxItem it && it.Tag is string eff)
        {
            _rdEffect = eff;
            _ = RdRefreshAsync(0);   // 选项少、点了就该立刻见变化，不等去抖
        }
    }

    /// <summary>代码侧选中某种边框效果（还原已有编辑 / 默认）。</summary>
    private void SelectRdEffect(string effect)
    {
        _rdEffect = effect;
        foreach (ComboBoxItem? it in RdEffectBox.Items)
        {
            if (it?.Tag is string tag && tag == effect)
            {
                if (!Equals(RdEffectBox.SelectedItem, it)) RdEffectBox.SelectedItem = it;
                return;
            }
        }
        // RD 没有棱彩（也没有该素材），老存档里存的 prismatic 一律落回不改边框
        _rdEffect = EffKeep;
        foreach (ComboBoxItem? it in RdEffectBox.Items)
        {
            if (it?.Tag is string tag && tag == EffKeep)
            {
                if (!Equals(RdEffectBox.SelectedItem, it)) RdEffectBox.SelectedItem = it;
                return;
            }
        }
    }

    private void RdAlpha_ValueChanged(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (!_ready) return;
        _rdAlpha = Math.Round(RdAlphaSlider.Value, 2);
        if (RdAlphaText is not null) RdAlphaText.Text = $"{_rdAlpha * 100:0}%";
        // 效果框单独一层，直接改它的不透明度即可，不重合成、不找引擎
        if (RdPanelImg is not null) RdPanelImg.Opacity = _rdAlpha;
    }

    // 拖动：主体图层本地平移，松手不需要重新合成
    private void RdCard_PointerPressed(object sender, PointerRoutedEventArgs e)
    {
        _rdDragging = true;
        _rdDragStart = e.GetCurrentPoint((Grid)sender).Position;
        _rdDx0 = _rdDx;
        _rdDy0 = _rdDy;
        ((Grid)sender).CapturePointer(e.Pointer);
        e.Handled = true;
    }

    private void RdCard_PointerMoved(object sender, PointerRoutedEventArgs e)
    {
        if (!_rdDragging) return;
        var pt = e.GetCurrentPoint((Grid)sender).Position;
        _rdDx = Clamp(_rdDx0 + (pt.X - _rdDragStart.X), -704, 704);
        _rdDy = Clamp(_rdDy0 + (pt.Y - _rdDragStart.Y), -1024, 1024);
        PlaceSubject();
        RdCaption.Text = $"位移 {_rdDx:0},{_rdDy:0}";
        e.Handled = true;
    }

    private void RdCard_PointerReleased(object sender, PointerRoutedEventArgs e)
    {
        if (!_rdDragging) return;
        _rdDragging = false;
        ((Grid)sender).ReleasePointerCapture(e.Pointer);
        e.Handled = true;
    }

    private void RdCard_PointerCaptureLost(object sender, PointerRoutedEventArgs e)
    {
        _rdDragging = false;
    }

    private async void RdApplyBtn_Click(object sender, RoutedEventArgs e)
    {
        if (_current is null || App.Engine is not { } engine) return;

        RdApplyBtn.IsEnabled = false;
        SetBusy(true, App.DryRun ? "正在写 RD 卡图（预演）…" : "正在写 RD 卡图…");
        SetLocked(true);
        try
        {
            await engine.CallAsync("rushduel.apply", new Dictionary<string, object?>
            {
                ["card"] = _current.Id,
                ["art"] = _rdArt,
                ["subject"] = _rdSubject,
                ["scale"] = RdScale,
                ["dx"] = (int)_rdDx,
                ["dy"] = (int)_rdDy,
                ["effect"] = _rdEffect,
                ["frame_alpha"] = _rdAlpha,
                ["dry_run"] = App.DryRun,
            });
            SetStatus(App.DryRun
                ? $"{_current.Title}：预演结束，没有写游戏文件"
                : $"{_current.Title}：Rush Duel 卡图已写入（完全退出游戏再重开才会加载）");
            if (!App.DryRun)
            {
                _ours = true;
                _registered = true;
                RdRevertBtn.IsEnabled = true;
                await RefreshGateAsync();
            }
            else
            {
                RdApplyBtn.IsEnabled = true;
            }
        }
        catch (EngineException ex) when (ex.Kind == "Canceled")
        {
            SetStatus("已取消");
            RdApplyBtn.IsEnabled = true;
        }
        catch (Exception ex)
        {
            SetStatus("写入失败：" + ex.Message);
            App.AddLog("ERROR", $"{_current.Title} RD 卡图写入失败：{ex.Message}");
            RdApplyBtn.IsEnabled = true;
        }
        finally
        {
            SetLocked(false);
            SetBusy(false, null);
        }
    }

    private async void RdRevertBtn_Click(object sender, RoutedEventArgs e)
    {
        if (_current is null || App.Engine is not { } engine) return;

        var dlg = new ContentDialog
        {
            Title = "撤回这张卡的 RD 卡图",
            Content = $"{_current.Title} 会回到原卡图，超框登记也一并撤掉。",
            PrimaryButtonText = "撤回",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Close,
            XamlRoot = XamlRoot,
        };
        if (await dlg.ShowAsync() != ContentDialogResult.Primary) return;

        SetBusy(true, App.DryRun ? "正在撤回（预演）…" : "正在撤回…");
        SetLocked(true);
        try
        {
            await engine.CallAsync("rushduel.remove", new Dictionary<string, object?>
            {
                ["card"] = _current.Id,
                ["dry_run"] = App.DryRun,
            });
            SetStatus(App.DryRun
                ? "预演结束，没有写游戏文件"
                : $"{_current.Title}：已回到原卡图");
            if (!App.DryRun)
            {
                _ours = false;
                RdRevertBtn.IsEnabled = false;
                await RefreshGateAsync();
            }
        }
        catch (EngineException ex)
        {
            SetStatus("撤回失败：" + ex.Message);
        }
        finally
        {
            SetLocked(false);
            SetBusy(false, null);
        }
    }
}
