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
    // 每次加载预览自增。这个流程里多次 await（问引擎要布局、读六七张图），
    // 期间用户可能又选了张卡或换了素材，于是两次加载交错跑：后发起的那次先回来，
    // 先发起的那次随后回来把界面覆盖回旧卡，素材、几何、滑块就串成一套错的。
    // 只在最后一次发起的加载里改界面，旧的跑完直接丢（2026-11-12 用户实测偶发）。
    private int _previewGen;
    // 用户在加载过程中选过主体：记下那一次的代次。加载收尾时会拿它判断
    // "这个主体是用户刚选的，别用引擎里的旧值盖掉、也别当垃圾清空"。
    private int _subjectPickedGen = -1;
    private bool _registered, _ours;
    private string? _artFrame, _artSubject;   // 卡图背景 / 主体（透明 PNG）
    private string _baseArt = "";             // 引擎给的当前卡图（背景兜底）
    private readonly HashSet<int> _officialIds = new();  // 官方超框的卡号
    private readonly HashSet<int> _ourIds = new();       // 本工具做的超框

    // 预览几何（卡片逻辑坐标 704×1024）
    private double _ax = 89, _ay = 191, _aw = 527, _ah = 528, _loreTop = 759;
    // 主体摆放基准 = 插图窗几何（引擎 compose 合成时一律用它），与 fullbleed 的
    // 整卡覆盖无关。满画幅卡若直接用被覆盖后的 _ax/_aw，选主体的默认大小与
    // 位置会按整卡算，跟实际合成结果对不上（2026-11-11 用户实测）。
    private double _subAx = 89, _subAy = 191, _subAw = 527, _subAh = 528;
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


        // 一进页面就是「没选卡」：空卡位、右栏收起。初始化时不会触发选中变化，
        // 不在这里主动收一次的话，控件会带着首屏一起亮出来。
        ClearDetail();

        _ready = true;

        // 四档互斥，没有「全部」。默认停在「官方超框」：这一档是卡图页最常看的内容，
        // 全量卡名表有一万四千多行，不先收窄反而没法浏览。
        ArtFilterBox.SelectedIndex = 0;
        _artFilterReady = true;
    }

    protected override async void OnNavigatedTo(NavigationEventArgs e)
    {
        base.OnNavigatedTo(e);
        // 页面被导航缓存留着，反复进页不能重复挂同一个处理
        App.MainBusyChanged -= OnMainBusy;
        App.MainBusyChanged += OnMainBusy;
        ApplyMainBusy();
        if (App.Engine is null)
        {
            SetStatus("引擎没有启动：" + (App.EngineError ?? "未知原因"));
            return;
        }
        // 写入/撤回进行中切回本页不重载：任务结束自己会刷新列表与预览，这里的重载
        // 只会把表单推回旧值、还抢走任务的状态条。首次进页（列表为空）照旧加载。
        if (App.MainBusy && _shown.Count > 0) return;

        // 无条件重载，且**先重读超框登记表**再查列表：安装整合包、恢复原版都发生在
        // 分享页，登记表一变，这里缓存的「官方 / 自改」两个集合就是旧快照。
        // 上次只重查了列表没重读集合，于是卡片数量是新的、标签还是旧的，重启才恢复
        // （2026-11-12 用户实测）。登记表查询首次约 2 秒、之后 5 毫秒。
        SetBusy(true, "正在读取超框登记表…");
        try
        {
            await RefreshGateAsync();
        }
        catch (Exception ex)
        {
            SetStatus("读超框登记表失败：" + ex.Message);
        }
        await SearchAsync("");
    }

    protected override void OnNavigatedFrom(NavigationEventArgs e)
    {
        base.OnNavigatedFrom(e);
        App.MainBusyChanged -= OnMainBusy;
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
    private const string LoadingHint = "正在读取卡片数据…";

    private void SearchBox_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (_ready) _ = SearchAsync(SearchBox.Text.Trim());
    }

    /// <summary>当前筛选档位，official / self / none / unreleased，不会为空。
    /// self 同时收「官方原本有」与「官方原本没有」两种自改（来历看行标签悬停）。</summary>
    private string ArtFilter => (ArtFilterBox.SelectedItem as ComboBoxItem)?.Tag as string ?? "";

    /// <summary>档位在界面上的文字，用于计数和空提示。</summary>
    private string ArtFilterText
        => (ArtFilterBox.SelectedItem as ComboBoxItem)?.Content as string ?? "";

    /// <summary>筛选下拉设默认值那一下也会触发 SelectionChanged，别在初始化时真去搜一次。</summary>
    private bool _artFilterReady;

    private void ArtFilterBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!_artFilterReady) return;
        _ = SearchAsync(SearchBox.Text.Trim());
    }

    /// <summary>刷新：按当前的搜索词和筛选重新拉一次列表，选中项会尽量保留。</summary>
    private async void RefreshButton_Click(object sender, RoutedEventArgs e)
        => await SearchAsync(SearchBox.Text.Trim());

    private async Task SearchAsync(string q)
    {
        // 引擎没起来时把进页时挂上的忙碌状态收掉，否则进度条一直转
        if (App.Engine is not { } engine)
        {
            SetBusy(false, null);
            return;
        }

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

        // 热搜索一毫秒就回来，底部状态条要是立刻弹出来只会闪一下。等过 200ms
        // 才亮提示，慢的那几次（首次进页、引擎前面还排着别的查询）才有反馈。
        _ = RevealHintAsync(cts.Token);
        SetBusy(true, null);
        try
        {
            var data = await engine.CallAsync("cards.search", new Dictionary<string, object?>
            {
                ["q"] = q,
                ["limit"] = SearchLimit,
                ["has_anim"] = false,
                // 超框状态筛选交给后端：列表只取前 200 条，在前端筛会把真正的结果截掉
                ["art"] = ArtFilter,
            });
            if (cts.IsCancellationRequested) return;

            // 重建列表会把选中清掉，先记住是哪张，重建完再放回去
            var keepId = _current?.Id ?? 0;
            _shown.Clear();
            foreach (var c in ParseCards(data))
            {
                c.OfficialArt = _officialIds.Contains(c.Id);
                c.OurArt = _ourIds.Contains(c.Id);
                _shown.Add(c);
            }
            if (keepId > 0 && _shown.FirstOrDefault(c => c.Id == keepId) is { } again)
                CardList.SelectedItem = again;
            // 计数用后端的 count（过滤后、截断前的总数）。「无超框」有一万四千多张，
            // 实际只取前 200 条，写成 200 会让人以为全游戏只有 200 张。
            var total = data.ValueKind == JsonValueKind.Object
                        && data.TryGetProperty("count", out var cv)
                        && cv.ValueKind == JsonValueKind.Number
                ? cv.GetInt32() : _shown.Count;
            // 筛选是常驻的，空列表时得说清是哪一档空了，否则看着像搜不到这张卡
            EmptyHint.Text = q.Length == 0
                ? $"「{ArtFilterText}」档里没有卡"
                : $"「{q}」不在「{ArtFilterText}」档里";
            EmptyHint.Visibility = _shown.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
            // 计数放在左栏里（不再占底部状态条），两页空闲时底部都折叠，面板高度才对得上
            StatusStrip.Visibility = Visibility.Collapsed;
            var suffix = $"（{ArtFilterText}）";
            if (total > _shown.Count) suffix += $" · 最多列 {SearchLimit} 张";
            CountText.Text = q.Length == 0
                ? $"共 {total} 张{suffix}"
                : $"「{q}」找到 {total} 张{suffix}";
        }
        catch (Exception ex)
        {
            SetStatus("搜索失败：" + ex.Message);
        }
        finally
        {
            cts.Cancel();          // 别让延时提示在结果回来之后才冒出来
            SetBusy(false, null);
        }
    }

    private async Task RevealHintAsync(CancellationToken token)
    {
        try
        {
            await Task.Delay(200, token);
        }
        catch (OperationCanceledException)
        {
            return;
        }
        if (token.IsCancellationRequested) return;
        StatusStrip.Visibility = Visibility.Visible;
        StatusText.Text = LoadingHint;
    }

    private static List<CardItem> ParseCards(JsonElement data)
    {
        var list = new List<CardItem>();
        if (data.ValueKind != JsonValueKind.Object ||
            !data.TryGetProperty("cards", out var arr) || arr.ValueKind != JsonValueKind.Array)
            return list;

        foreach (var c in arr.EnumerateArray())
        {
            // 占位副本号（real != 0）：游戏不渲染这些号，写超框也不会生效，
            // 列表里只剩一行点不动的灰条。全游戏 22 张，直接不列。
            if (c.TryGetProperty("real", out var rr) && rr.ValueKind == JsonValueKind.Number
                && rr.GetInt32() > 0)
                continue;
            list.Add(new CardItem
            {
                Id = c.TryGetProperty("id", out var id) ? id.GetInt32() : 0,
                Name = c.TryGetProperty("name", out var n) && n.ValueKind == JsonValueKind.String
                    ? n.GetString() ?? "" : "",
                Real = c.TryGetProperty("real", out var r) ? r.GetInt32() : 0,
                Unreleased = c.TryGetProperty("unreleased", out var u) && u.ValueKind == JsonValueKind.True,
                UnknownSubtype = c.TryGetProperty("unknown_subtype", out var us) && us.ValueKind == JsonValueKind.True,
                Locked = c.TryGetProperty("locked", out var lk) && lk.ValueKind == JsonValueKind.True,
                SubtypeCn = c.TryGetProperty("subtype_cn", out var cn) && cn.ValueKind == JsonValueKind.String
                    ? cn.GetString() ?? "" : "",
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
        // 容器会被回收复用，两个方向都要显式设回去。占位卡、未上线卡、
        // 卡型未确认的卡共用灰态，锁定原因由标签的悬停提示说明。
        bool dim = card.IsPlaceholder || card.Locked;
        box.IsEnabled = !dim;
        box.Opacity = dim ? 0.45 : 1.0;
    }

    private async void CardList_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (CardList.SelectedItem is not CardItem item)
        {
            ClearDetail();
            return;
        }
        if (item.IsPlaceholder)
        {
            // 灰掉之后本来就点不到，兜一层防止别处把它选中
            CardList.SelectedItem = null;
            SetStatus($"{item.Title}：#{item.Id} 是占位号，游戏实际渲染 #{item.Real}");
            return;
        }
        // 未上线、卡型未确认的卡照样选中：它们有卡图，能看，只是不给改。
        // 以前这里直接弹回选中，结果标题回到「选一张卡」而预览还留着上一张的图。
        _current = item;
        CardName.Text = item.Title;
        CardMeta.Text = string.IsNullOrEmpty(item.SubtypeCn)
            ? $"#{item.Id}" : $"#{item.Id} · {item.SubtypeCn}";
        ResetArt();
        await LoadCurrentAsync(item);
    }

    /// <summary>
    /// 右栏回到「没选卡」：标题占位、预览清空、控件收起。
    ///
    /// 切分类或搜索把当前卡过滤掉时都会走到这里。以前只清了标题，卡图和控件留在
    /// 原地，切到新分类后看着就像「这张卡长这样」，很容易认错。
    /// </summary>
    private void ClearDetail()
    {
        _current = null;
        CardName.Text = "选一张卡";
        CardMeta.Text = "";
        ResetArt();
        CardView.Visibility = Visibility.Collapsed;
        EmptySlot.Visibility = Visibility.Visible;
        PreviewHint.Text = "从左边选一张卡";
        PreviewHint.Visibility = Visibility.Visible;
        PreviewCaption.Text = "";
        _registered = _ours = false;
        ApplyBtn.IsEnabled = false;
        RestoreButton.IsEnabled = false;
        SetEditable(false);
    }

    /// <summary>
    /// 右栏控件区的三种排布：
    ///   可写 = 控件、刷新、复原都在；
    ///   只看 = 控件收起，原地换一句说明（未上线 / 卡型未确认 / 数据读不出来）；
    ///   未选中 = 全部收起，右栏空着。
    /// </summary>
    private void SetEditable(bool editable, string? noteTitle = null, string? note = null)
    {
        EditPane.Visibility = editable ? Visibility.Visible : Visibility.Collapsed;
        RefreshButton.Visibility = editable ? Visibility.Visible : Visibility.Collapsed;
        RestoreButton.Visibility = editable ? Visibility.Visible : Visibility.Collapsed;
        ReadOnlyNote.Visibility = note is null ? Visibility.Collapsed : Visibility.Visible;
        if (note is not null)
        {
            ReadOnlyTitle.Text = noteTitle ?? "";
            ReadOnlyText.Text = note;
        }
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
        RestoreButton.IsEnabled = false;
        // 切卡时先把上一张的图收起来，读回来之前只留一句提示
        CardView.Visibility = Visibility.Collapsed;
        EmptySlot.Visibility = Visibility.Collapsed;
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
                PreviewCaption.Text = "";
                SetEditable(false);
                return;
            }

            _registered = data.TryGetProperty("registered", out var rg) && rg.GetBoolean();
            _ours = data.TryGetProperty("ours", out var ou) && ou.GetBoolean();
            var w = data.TryGetProperty("width", out var wv) ? wv.GetInt32() : 0;
            var h = data.TryGetProperty("height", out var hv) ? hv.GetInt32() : 0;
            PreviewCaption.Text = $"{w} × {h}";

            // 卡图照常画，能不能改由 locked 决定：未上线、卡型未确认、卡数据读不出来
            // 这三种都不该给控件，写下去游戏要么不认，要么画错卡框。
            var locked = data.TryGetProperty("locked", out var lk) && lk.GetBoolean();
            var unreleased = data.TryGetProperty("unreleased", out var ur) && ur.GetBoolean();
            var unknown = data.TryGetProperty("unknown_subtype", out var us) && us.GetBoolean();
            if (locked)
                SetEditable(false,
                    unknown ? "卡型未确认" : unreleased ? "未上线" : "这张卡暂不可写",
                    unknown ? "游戏里出现了工具还不认识的卡型，这一版不处理，写下去卡框可能画错。"
                            : unreleased ? "这张卡还没上线，游戏里看不到，改不了超框。"
                            : "卡数据没读出来，先修好游戏数据再来。");
            else
                SetEditable(true);

            await SetupPreviewAsync(item.Id);

            RestoreButton.IsEnabled = _ours;
            ApplyBtn.IsEnabled = true;
        }
        catch (Exception ex)
        {
            PreviewHint.Text = "读不了这张卡：" + ex.Message;
        }
    }

    /// <summary>拉预览几何 + 卡框图 + 底色卡图，搭好本地分层面板。</summary>
    private async Task SetupPreviewAsync(int card, bool keepArt = false)
    {
        if (App.Engine is not { } engine) return;
        var gen = ++_previewGen;
        // 刚在界面上选过素材时把它们带进来，下面的加载不会把它们清掉。
        // 卡图背景和主体都要保：重选卡图背景会重跑整个加载，has_edit=false 分支
        // 会把主体当垃圾清掉（2026-11-11 用户实测：选主体后再选卡图背景，主体消失）。
        var savedArt = keepArt ? _artFrame : null;
        var savedSubject = keepArt ? _artSubject : null;
        var savedSubjectSource = keepArt ? SubjectImg.Source : null;
        var savedSubW = keepArt ? _subW : 0;
        var savedSubH = keepArt ? _subH : 0;
        var savedSubjectName = keepArt ? SubjectBox.Text : null;
        // 三个本地已调参数与素材同生共死: 主体大小、位移、效果框透明度。
        // 重载时 has_edit 分支会把它们打回引擎值或默认（用户实测: 大小回 1.0×），
        // keepArt 时暂存眼前状态, 恢复段原样放回。
        var savedScale = keepArt ? SubjectScaleSlider.Value : 1.0;
        var savedDx = keepArt ? _dx : 0;
        var savedDy = keepArt ? _dy : 0;
        var savedLoreAlpha = keepArt ? LoreAlphaSlider.Value : 1.0;
        var lay = await engine.CallAsync("overframe.layout", new Dictionary<string, object?>
        {
            ["card"] = card,
        });
        if (lay.ValueKind != JsonValueKind.Object)
        {
            PreviewHint.Text = "读不出预览信息";
            return;
        }
        // 这次已经过期（用户又选了别的卡/素材），别再往下改界面
        if (gen != _previewGen) return;

        _ax = lay.TryGetProperty("ax", out var v1) ? v1.GetDouble() : 89;
        _ay = lay.TryGetProperty("ay", out var v2) ? v2.GetDouble() : 191;
        _aw = lay.TryGetProperty("aw", out var v3) ? v3.GetDouble() : 527;
        _ah = lay.TryGetProperty("ah", out var v4) ? v4.GetDouble() : 528;
        _loreTop = lay.TryGetProperty("lore_top", out var v5) ? v5.GetDouble() : 759;
        _cardW = lay.TryGetProperty("card_w", out var v6) ? v6.GetDouble() : 704;
        _cardH = lay.TryGetProperty("card_h", out var v7) ? v7.GetDouble() : 1024;
        _overflow = lay.TryGetProperty("overflow_scale", out var v8) ? v8.GetDouble() : 1.38;
        _baseArt = lay.TryGetProperty("base_art", out var v9) ? v9.GetString() ?? "" : "";
        // 满画幅超框卡：插画本来就是整张卡面，预览铺满整张卡，
        // 不叠卡框和文字栏，这样看到的就是游戏里的样子。
        var fullbleed = lay.TryGetProperty("fullbleed", out var fbv) && fbv.GetBoolean();
        _loreX = lay.TryGetProperty("lore_x", out var lx) ? lx.GetDouble() : 30;
        _loreY = lay.TryGetProperty("lore_y", out var ly) ? ly.GetDouble() : 750;
        _loreW = lay.TryGetProperty("lore_w", out var lw) ? lw.GetDouble() : 646;
        _loreH = lay.TryGetProperty("lore_h", out var lh) ? lh.GetDouble() : 224;
        _loreBottom = lay.TryGetProperty("lore_bottom", out var lb) ? lb.GetDouble() : 974;

        // 插图窗（幻彩是左右满幅，具体几何在 ApplyArtRect 里按效果取）

        // 主体图层只裁到卡片下沿（可从效果框下穿过，在卡底显示）
        SubjectLayer.Clip = new RectangleGeometry { Rect = new Rect(0, 0, _cardW, _cardH) };

        // 主体摆放基准锁定为插图窗几何：fullbleed 分支接下来会把 _ax/_aw 覆盖成
        // 整卡，但引擎 compose 合成时主体一律按插图窗摆（OF.compose 的 cover/x
        // 都用 art_box），预览必须用同一套基准，两边才一致。
        _subAx = _ax;
        _subAy = _ay;
        _subAw = _aw;
        _subAh = _ah;

        // 上次应用过就自动加载素材与参数，方便在原有基础上继续调
        var hasEdit = lay.TryGetProperty("has_edit", out var he) && he.GetBoolean();
        // 这几次 await 期间用户可能刚选过主体，那是最新的意图，不能被引擎里的旧值覆盖
        var pickedHere = _subjectPickedGen == gen;
        if (hasEdit)
        {
            _artFrame = _baseArt.Length > 0 ? _baseArt : null;
            var aname = lay.TryGetProperty("edit_art_name", out var an) ? an.GetString() : null;
            FrameArtBox.Text = !string.IsNullOrEmpty(aname)
                ? aname
                : (_baseArt.Length > 0 ? Path.GetFileName(_baseArt) : "");

            // 本地刚选的主体优先于引擎里保存的旧值（引擎值是上次应用的旧状态）
            var subj = lay.TryGetProperty("edit_subject", out var sj) ? sj.GetString() : null;
            if (savedSubject is null && !pickedHere && !string.IsNullOrEmpty(subj) && File.Exists(subj))
            {
                _artSubject = subj;
                var sbmp = await LoadFileBitmapAsync(subj);
                if (gen != _previewGen) return;
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
            // 这张卡没应用过。keepArt=true 时暂存值就是用户眼前的状态，
            // 直接套用暂存值而不是先清成默认再等恢复段放回: 清完到放回之间
            // 隔着几次 await，UI 渲染出"主体没了"的中间帧，就是那一下闪烁。
            _artFrame = savedArt;
            FrameArtBox.Text = savedArt is not null ? Path.GetFileName(savedArt) : "";
            _artSubject = savedSubject;
            SubjectBox.Text = savedSubject is not null ? savedSubjectName ?? "" : "";
            if (savedSubjectSource is not null)
            {
                _subW = savedSubW;
                _subH = savedSubH;
                SubjectImg.Source = savedSubjectSource;
            }
            else if (!pickedHere)
            {
                // 加载期间用户选过主体就留着，别当垃圾清掉
                _subW = _subH = 0;
                SubjectImg.Source = null;
            }
            SubjectScaleSlider.Value = savedScale;
            _dx = savedDx;
            _dy = savedDy;
            LoreAlphaSlider.Value = savedLoreAlpha;
        }

        // 用户刚选的素材优先于上面两条分支的结论
        if (savedArt is not null)
        {
            _artFrame = savedArt;
            FrameArtBox.Text = Path.GetFileName(savedArt);
        }
        if (savedSubject is not null && savedSubjectSource is not null)
        {
            _artSubject = savedSubject;
            _subW = savedSubW;
            _subH = savedSubH;
            SubjectImg.Source = savedSubjectSource;
            SubjectBox.Text = savedSubjectName;
        }
        // 本地已调参数无条件放回: keepArt=true 时暂存值就是用户眼前的状态，
        // 不管它来自引擎上次恢复还是刚拖过的滑块。放在 580 行 Opacity 赋值之前，
        // 效果框透明度会自然跟上。
        if (keepArt)
        {
            SubjectScaleSlider.Value = savedScale;
            _dx = savedDx;
            _dy = savedDy;
            LoreAlphaSlider.Value = savedLoreAlpha;
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
        // 六张图读完，用时最久的一段。这里过期就直接收手，后面全是改界面的动作
        if (gen != _previewGen) return;

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
        if (gen != _previewGen) return;
        LoreImg.Opacity = LoreAlphaSlider.Value;

        // 背景：用户选的优先，否则用引擎给的当前卡图
        var art = _artFrame ?? (_baseArt.Length > 0 && File.Exists(_baseArt) ? _baseArt : null);

        // 满画幅只在「还没动过这张卡」时生效：一选素材就是要改它了，
        // 这时必须看到卡框，否则背景铺满整卡，看不出改成了什么样。
        if (fullbleed && _artFrame is not null) fullbleed = false;
        if (fullbleed)
        {
            _ax = 0;
            _ay = 0;
            _aw = _cardW;
            _ah = _cardH;
        }
        FrameImg.Visibility = fullbleed ? Visibility.Collapsed : Visibility.Visible;
        LoreImg.Visibility = fullbleed ? Visibility.Collapsed : Visibility.Visible;
        // 引擎给的底图已经按插图窗裁过，坐标系和窗一致，直接拉伸填满；
        // 用户自己选的素材要保持比例，用填满裁切。
        ArtBrush.Stretch = fullbleed || _artFrame is null
            ? Microsoft.UI.Xaml.Media.Stretch.Fill
            : Microsoft.UI.Xaml.Media.Stretch.UniformToFill;
        // 几何可能刚被 fullbleed 改过，必须立刻应用到 ArtRect，
        // 否则它还是上一次的尺寸，图会被压进一个小方块里。
        ApplyArtRect();

        if (art is not null)
        {
            ArtBrush.ImageSource = await LoadFileBitmapAsync(art);
            if (gen != _previewGen) return;
            PreviewHint.Visibility = Visibility.Collapsed;
        }
        else
        {
            ArtBrush.ImageSource = null;
            PreviewHint.Text = "选一张卡图背景就能预览（这张卡的插图不在本地）";
            PreviewHint.Visibility = Visibility.Visible;
        }

        // 几何和图层都搭好了，把空卡位换成真卡图
        CardView.Visibility = Visibility.Visible;
        EmptySlot.Visibility = Visibility.Collapsed;

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
        var cover = Math.Max(_subAw / _subW, _subAh / _subH);
        var r = cover * _overflow * Math.Max(0.1, SubjectScaleSlider.Value);
        var sw = _subW * r;
        var sh = _subH * r;
        var sx = _subAx + (_subAw - sw) / 2 + _dx;
        var sy = _subAy + (_subAh - sh) / 2 + _dy;
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
        // 走完整的加载流程：几何、填充方式、卡框可见性都由那一处统一决定。
        // 原来这里只换了图片，满画幅卡留下的 704×1024 画布和 Fill 都还在，
        // 素材会被拉满整张卡，普通卡也会因为 Stretch 没更新而变形。
        if (_current is not null)
            await SetupPreviewAsync(_current.Id, keepArt: true);
        else
        {
            ArtBrush.ImageSource = await LoadFileBitmapAsync(f);
            PreviewHint.Visibility = Visibility.Collapsed;
        }
    }

    private async void PickSubject_Click(object sender, RoutedEventArgs e)
    {
        var f = await PickImageAsync();
        if (f is null) return;
        _artSubject = f;
        // 记下这次是"哪一轮加载期间选的"：加载如果还在跑，收尾时不能拿旧状态盖掉它
        _subjectPickedGen = _previewGen;
        SubjectBox.Text = Path.GetFileName(f);
        // 选了新主体 = 新的合成起点：倍率与位移回到默认。edits 里"接着调"的
        // 旧参数对应的是上一张主体的尺寸，套到新图上大小位置全不对。
        SubjectScaleSlider.Value = 1;
        _dx = _dy = 0;
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
        if (file is null) return null;
        // 过滤器能绕过（粘贴完整路径、改扩展名），这里按内容再确认一次
        if (!await ImportCheck.EnsureAsync(XamlRoot, file.Path, "image")) return null;
        return file.Path;
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

        // 不能连续生成（2026-11-17 用户定稿，与动画页/特效页同一口径）：
        // 这张卡上已经挂着本工具写的超框（含整合包装入的）时，必须先复原。
        // 预演不写游戏文件，_ours 也不会被置真，所以预演可以连着跑。
        if (_ours)
        {
            await Dialogs.InfoAsync(XamlRoot, "已经挂着超框了",
                "这张卡上的超框是本工具加的。要换的话，先点右上角的「复原」。");
            return;
        }

        var hasArt = _artFrame is not null || (_baseArt.Length > 0 && File.Exists(_baseArt));
        if (!hasArt)
        {
            SetStatus("先选一张卡图背景（这张卡的插图不在本地）");
            return;
        }

        var id = _current.Id;
        var title = _current.Title;
        var meta = string.IsNullOrEmpty(_current.SubtypeCn)
            ? _current.IdText
            : $"{_current.IdText} · {_current.SubtypeCn}";
        // 所有档位都问一次。只有「官方超框」会顶掉游戏自带的超框，用覆盖专用文案；
        // 自改★（官方那套已被工具覆盖）与自改、无超框都写自己的东西，用普通文案。
        // 按卡片状态判，不按筛选档位名写死。
        var official = _officialIds.Contains(id) && !_ourIds.Contains(id);
        var ok = official
            ? await Dialogs.ConfirmAsync(XamlRoot, "覆盖官方超框？",
                "自带官方超框。继续会换成你的版本，官方那套不再显示。",
                "生成超框", title, meta, "之后点「复原」可以还原。")
            : await Dialogs.ConfirmAsync(XamlRoot, "生成超框？",
                "按当前预览生成一套超框并写入游戏。",
                "生成超框", title, meta, "之后点「复原」可以还原。");
        if (!ok) return;

        ApplyBtn.IsEnabled = false;
        SetBusy(true, App.DryRun ? "正在写卡图（预演）…" : "正在写卡图…");
        SetLocked(true);
        // 结果框留到 finally 之后弹，遮罩亮着时不隔着一层灰说「完成」。
        string? resultTitle = null, resultBody = null;
        bool resultDanger = false;
        var resultDone = App.DryRun ? "预演结束，没有写游戏文件" : $"{title}：超框已写入";
        try
        {
            var data = await engine.CallAsync("overframe.apply", new Dictionary<string, object?>
            {
                ["card"] = id,
                ["art"] = _artFrame,
                ["subject"] = _artSubject,
                ["scale"] = Scale,
                ["dx"] = (int)_dx,
                ["dy"] = (int)_dy,
                ["lore_alpha"] = Alpha,
                ["effect"] = _effect,
                ["dry_run"] = App.DryRun,
            });
            // 卡图只在游戏本体里（190 张初始卡）时，这次写的是缓存里新造的一份。
            // 写完再读就变成缓存命中了，所以这个标记要从写入的返回里取。
            var nativeSource = data.ValueKind == JsonValueKind.Object
                               && data.TryGetProperty("native_source", out var ns) && ns.GetBoolean();

            if (!App.DryRun)
            {
                _ours = true;
                _registered = true;
                RestoreButton.IsEnabled = true;
                // 后面还有刷新列表和预览两步。这段时间遮罩还亮着，先说「已写入」
                // 就成了「写着写着突然说写完了却还卡着」，用户会当成假死。
                SetStatus("正在刷新预览…");
                await RefreshGateAsync();
                await FollowTagAsync(id);
            }
            resultBody = App.DryRun
                ? "流程跑通了，没有写游戏文件。"
                : nativeSource
                    ? $"{title} 的超框已写入。这张卡的卡图在游戏本体，已补到当前账号缓存。"
                    : $"{title} 的超框已写入。完全退出游戏再重开才会加载。";
            resultTitle = App.DryRun ? "预演完成" : "完成";
        }
        catch (EngineException ex) when (ex.Kind == "Canceled")
        {
            await SettleCanceledAsync();
        }
        catch (Exception ex)
        {
            SetStatus("写入失败：" + ex.Message);
            App.AddLog("ERROR", $"{title} 超框写入失败：{ex.Message}");
            ApplyBtn.IsEnabled = true;
            resultTitle = "失败";
            resultBody = $"游戏文件没有改动。\n{ex.Message}";
            resultDanger = true;
        }
        finally
        {
            SetLocked(false);
            SetBusy(false, null);
        }
        // 遮罩撤掉之后再弹结果框。SetBusy(false) 只在文字还是加载提示时收状态条，
        // 这里补一次最终结果，保证底部与弹窗说的一致。
        if (resultTitle is not null)
        {
            SetStatus(resultDanger ? "写入失败" : resultDone);
            await Dialogs.InfoAsync(XamlRoot, resultTitle, resultBody ?? "", resultDanger);
        }
    }

    private async void RevertBtn_Click(object sender, RoutedEventArgs e)
    {
        if (_current is null || App.Engine is not { } engine) return;

        if (!await Dialogs.ConfirmAsync(XamlRoot, "复原超框",
            "回到本工具改动之前的样子，超框登记一并撤掉。",
            "复原", _current.Title,
            string.IsNullOrEmpty(_current.SubtypeCn)
                ? _current.IdText
                : $"{_current.IdText} · {_current.SubtypeCn}",
            "这一步不能撤销。"))
            return;

        var id = _current.Id;
        var title = _current.Title;
        SetBusy(true, App.DryRun ? "正在撤回（预演）…" : "正在撤回…");
        SetLocked(true);
        string? resultTitle = null, resultBody = null;
        bool resultDanger = false;
        try
        {
            await engine.CallAsync("overframe.remove", new Dictionary<string, object?>
            {
                ["card"] = id,
                ["dry_run"] = App.DryRun,
            });
            if (!App.DryRun)
            {
                _ours = false;
                RestoreButton.IsEnabled = false;
                // 同写入：撤回也要刷新列表和预览，这期间遮罩还在，先别说已完成
                SetStatus("正在刷新预览…");
                await RefreshGateAsync();
                await FollowTagAsync(id);
            }
            resultTitle = App.DryRun ? "预演完成" : "完成";
            resultBody = App.DryRun ? "流程跑通了，没有写游戏文件。" : $"{title} 已回到原卡图。";
        }
        catch (EngineException ex) when (ex.Kind == "Canceled")
        {
            await SettleCanceledAsync();
        }
        catch (EngineException ex)
        {
            SetStatus("撤回失败：" + ex.Message);
            resultTitle = "失败";
            resultBody = $"游戏文件没有改动。\n{ex.Message}";
            resultDanger = true;
        }
        finally
        {
            SetLocked(false);
            SetBusy(false, null);
        }
        if (resultTitle is not null)
        {
            SetStatus(resultDanger ? "撤回失败"
                : App.DryRun ? "预演结束，没有写游戏文件" : $"{title}：已回到原卡图");
            await Dialogs.InfoAsync(XamlRoot, resultTitle, resultBody ?? "", resultDanger);
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

    /// <summary>
    /// 主任务槽被别的页面占着时锁住参数面板与刷新/复原。
    /// 本页自己跑任务时整页有遮罩，这里跟着锁不冲突；左栏列表与搜索照常可用。
    /// WinUI 3 里只有 Control 才有 IsEnabled，Panel 没有，所以用「关掉命中测试 + 压暗」
    /// 表达禁用。这样也不会覆盖复原按钮自己的可用状态（_ours），解锁时不必猜该恢复成什么。
    /// </summary>
    private void ApplyMainBusy()
    {
        bool busy = App.MainBusy;
        EditPane.IsHitTestVisible = !busy;
        EditPane.Opacity = busy ? 0.5 : 1;
        CardActions.IsHitTestVisible = !busy;
        CardActions.Opacity = busy ? 0.5 : 1;
        MainBusyHint.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
    }

    /// <summary>事件从引擎读循环那条线程发出来，必须转回界面线程再动控件。</summary>
    private void OnMainBusy()
    {
        DispatcherQueue?.TryEnqueue(ApplyMainBusy);
    }

    /// <summary>遮罩上的取消。整页锁住之后别的都点不到了，这里留一个出口。</summary>
    private async void OverlayCancel_Click(object sender, RoutedEventArgs e)
    {
        if (App.Engine is { } engine) await engine.CallAsync("cancel");
    }

    /// <summary>
    /// 任务被取消。取消可能正好停在写文件中间，界面状态不能猜，重读一次再报结果。
    /// 不重读的话会出现「按钮灰着回不来」或者「显示成功其实只写了一半」。
    /// </summary>
    private async Task SettleCanceledAsync()
    {
        SetStatus("已取消，正在核对卡片状态…");
        try
        {
            await RefreshGateAsync();
            if (_current is { } cur) await LoadCurrentAsync(cur);
            SetStatus("已取消");
        }
        catch (Exception ex)
        {
            SetStatus("已取消，但状态没读回来：" + ex.Message);
        }
    }

    /// <summary>
    /// 写入 / 复原之后把列表跟到这张卡的新档位。
    ///
    /// 三档（不含未上线）：写入让卡从「官方超框」或「无超框」都变成「自改超框」，
    /// 复原再变回去。当前档不含它时列表里就没有这张卡了，不跟档等于把用户正在看的卡弄丢。
    /// 「未上线」档里的卡写完仍是未上线，不跨档，只重拉一次把行标签刷新。
    /// </summary>
    private async Task FollowTagAsync(int cardId)
    {
        if (ArtFilter != "unreleased")
        {
            var official = _officialIds.Contains(cardId);
            var ours = _ourIds.Contains(cardId);
            // selfOfficial 与 self 在界面上已并成一档（2026-11-17）
            var want = ours ? "self" : official ? "official" : "none";
            if (want != ArtFilter)
            {
                for (var i = 0; i < ArtFilterBox.Items.Count; i++)
                {
                    if ((ArtFilterBox.Items[i] as ComboBoxItem)?.Tag as string != want) continue;
                    // 换档会触发 SelectionChanged 再搜一遍，下面自己 await，先按住
                    _artFilterReady = false;
                    ArtFilterBox.SelectedIndex = i;
                    _artFilterReady = true;
                    break;
                }
            }
        }

        await SearchAsync(SearchBox.Text.Trim());
        if (CardList.SelectedItem is CardItem hit)
        {
            // 列表刚换过数据，容器还没生成，直接 ScrollIntoView 算不出位置
            CardList.UpdateLayout();
            CardList.ScrollIntoView(hit);
        }
    }

    private void SetBusy(bool busy, string? text)
    {
        CardList.IsEnabled = !busy;
        Progress.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
        Progress.IsIndeterminate = busy;
        if (text is not null) SetStatus(text);
        // 只收起自己那句加载提示，别把「搜索失败」「写入完成」这类状态一起抹掉
        if (!busy && StatusText.Text == LoadingHint)
            StatusStrip.Visibility = Visibility.Collapsed;
    }


}
