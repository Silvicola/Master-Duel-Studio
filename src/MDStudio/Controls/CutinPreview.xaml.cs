using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Threading.Tasks;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Media;
using SkiaSharp;
using SkiaSharp.Views.Windows;
using Windows.Media.Core;
using Windows.Media.Playback;
using Windows.Storage;

namespace MDStudio.Controls;

/// <summary>
/// 自改动画的预览播放器。
///
/// 只播我们自己生成的那套东西：N 张整帧图 + 一条缩放曲线。所以不需要 Spine 运行时，
/// 也不需要解算骨骼，按时间选第几帧、乘曲线给的缩放画出来就是游戏里的样子。
/// 帧图来自生成时存下的缓存（长边 900 的 WebP），不读游戏里的 15360² 图集。
///
/// 画布锁 16:9：游戏里召唤动画铺满 16:9 全屏，画布不是 16:9 时角色比例会随窗口变形
/// （2026-11-20 用户实测：对话框被卡成竖条，背景被裁成中间一条）。
/// </summary>
public sealed partial class CutinPreview : UserControl
{
    // 画布高度等于多少个骨骼单位。原来按 prefab 的 camera2DSize 推成 5000，
    // 但召唤场景的相机比卡面 prefab 更近：实测 2930 与 1465 两档录像各自反推，
    // 可见高度都是 2910 单位上下（2026-11-21 用户录屏对比，两档结果相差 0.1%）。
    // 游戏按「角色尺寸」等比缩放，所以这是个固定常数。
    private const double UnitsToPixels = 2880.0;
    private const double Aspect = 16.0 / 9.0;

    // 帧图按需解码：打开时只解第一张，播到哪解到哪，离得远的释放。
    // 一次全解的话 300 帧 900 长边要吃 550MB，打开也要等几百毫秒。
    private readonly Dictionary<int, SKBitmap> _cache = new();
    private readonly object _cacheGate = new();
    private string _frameDir = "";
    private int _frameCount;
    private SKBitmap? _field;

    // 当前帧前后各留这么多张，900 长边一张 1.74MB，±14 帧约 50MB
    private const int KeepFrames = 14;

    private double _duration = 1.6;
    private double _sizeUnits = 4096;   // 兜底：meta 缺这个字段时按 4096 画，宁大不小
    private double _zoom = 1;
    // 左右移 / 上下移按「角色高度倍数」记，旋转按角度（正数逆时针），与界面同口径
    private double _cutOffX, _cutOffY, _cutRot;

    // 特效（runfx）那套参数：画面大小是「占屏高的比例」，偏移按界面上的百分比记。
    // 生成时传给引擎的是世界单位，离开补偿曲线就读不懂，所以 meta 里另存了百分比。
    private string _kind = "cutin";
    private double _size = 0.5;
    private double _offXPct, _offYPct, _rot;
    private double _time;
    private bool _playing = true;
    private bool _loop = true;
    private bool _blackBg;
    private bool _loaded;
    private bool _syncingSeek;
    private double _speed = 1;

    private double[] _curveFrames = Array.Empty<double>();
    private double[] _curveScales = Array.Empty<double>();
    private double _curveLast = 1;

    // ---- 音效 ----
    // 游戏里 SE 比画面晚约 0.3 秒开始（2026-11-21 三段录像波形对齐，0.23 / 0.26 / 0.33），
    // 预览补上这一下才和游戏里听到的一致。
    private const double SeDelay = 0.3;
    private const string FieldGlyph = "\uE790";
    private const string BlackGlyph = "\uE7B3";
    private const string MuteGlyph = "\uE74F";
    private const string SoundGlyph = "\uE767";
    private string _seLabel = "";
    private MediaPlayer? _soundPlayer;
    private bool _soundOn;

    // 自己推时钟。CompositionTarget.Rendering 只在合成器有帧要出时才回调，
    // 对话框第二次打开、没有入场动画在跑时它不来了，画面就停在第 0 帧（2026-11-20 实测）。
    private readonly DispatcherQueueTimer _timer;
    private readonly Stopwatch _clock = new();
    private TimeSpan _lastTick;
    private int _lastFrame = -1;

    /// <summary>
    /// 画面换帧时通知外面：当前第几帧（从 1 数）与总帧数。
    /// 只在帧号真的变了才触发，标题条不用每 16ms 重写一遍文字。
    /// </summary>
    public event Action<int, int>? FrameChanged;

    public CutinPreview()
    {
        InitializeComponent();
        _timer = DispatcherQueue.CreateTimer();
        _timer.Interval = TimeSpan.FromMilliseconds(16);
        _timer.IsRepeating = true;
        _timer.Tick += (_, _) => Advance();
    }

    /// <summary>读这张卡的缓存并开始播放。帧一张都读不到时返回 false。</summary>
    public async Task<bool> OpenAsync(int card, string kind = "cutin")
    {
        if (App.Engine is not { } engine) return false;
        _kind = kind == "runfx" ? "runfx" : "cutin";
        JsonElement data;
        try
        {
            data = await engine.CallAsync("preview.meta", new { card, kind = _kind });
        }
        catch
        {
            return false;
        }
        if (data.ValueKind != JsonValueKind.Object ||
            !data.TryGetProperty("frames", out var framesEl))
            return false;

        int count = framesEl.GetInt32();
        if (count <= 0) return false;
        if (!data.TryGetProperty("dir", out var dirEl)) return false;
        string dir = dirEl.GetString() ?? "";
        _duration = data.TryGetProperty("duration", out var dEl) ? dEl.GetDouble() : 1.6;
        if (_duration <= 0) _duration = 1.6;
        _sizeUnits = data.TryGetProperty("size_units", out var sEl) ? sEl.GetDouble() : 4096;
        // 放大倍数乘在骨架的缩放关键帧上，预览不跟着乘就会比游戏里小
        _zoom = data.TryGetProperty("zoom", out var zEl) ? zEl.GetDouble() : 1.0;
        if (_zoom <= 0) _zoom = 1.0;

        // 特效用的四个数，动画那份没有就用默认值，互不干扰
        _size = data.TryGetProperty("size", out var szEl) ? szEl.GetDouble() : 0.5;
        if (_size <= 0) _size = 0.5;
        _offXPct = data.TryGetProperty("offx_pct", out var oxEl) ? oxEl.GetDouble() : 0;
        _offYPct = data.TryGetProperty("offy_pct", out var oyEl) ? oyEl.GetDouble() : 0;
        _rot = data.TryGetProperty("rotation", out var rtEl) ? rtEl.GetDouble() : 0;
        _cutOffX = data.TryGetProperty("offx", out var coxEl) ? coxEl.GetDouble() : 0;
        _cutOffY = data.TryGetProperty("offy", out var coyEl) ? coyEl.GetDouble() : 0;
        _cutRot = data.TryGetProperty("rotate", out var crtEl) ? crtEl.GetDouble() : 0;

        _curveFrames = Array.Empty<double>();
        _curveScales = Array.Empty<double>();
        if (data.TryGetProperty("curve", out var curveEl) && curveEl.ValueKind == JsonValueKind.Array)
        {
            var fs = new List<double>();
            var ss = new List<double>();
            foreach (var pt in curveEl.EnumerateArray())
            {
                if (pt.ValueKind != JsonValueKind.Array) continue;
                if (pt.GetArrayLength() < 2) continue;
                fs.Add(pt[0].GetDouble());
                ss.Add(pt[1].GetDouble());
            }
            if (fs.Count > 0)
            {
                _curveFrames = fs.ToArray();
                _curveScales = ss.ToArray();
                _curveLast = Math.Max(_curveFrames[^1], 1);
            }
        }

        ClearFrames();
        _frameDir = dir;
        _frameCount = count;
        // 先解第一张，确认缓存文件确实在；其余等播到再解
        if (EnsureFrame(0) is null)
        {
            _frameCount = 0;
            return false;
        }

        // 每次打开都回到默认观感：场地背景、1 倍速、音效关
        _blackBg = false;
        BgIcon.Glyph = FieldGlyph;
        _speed = 1;
        SpeedText.Text = "1×";

        _field ??= LoadAsset("Assets/Preview/cutin-field.png");
        _time = 0;
        _lastFrame = -1;
        _playing = true;
        Seek.Maximum = _duration;
        _loaded = true;
        _syncingSeek = true;
        Seek.Value = 0;
        _syncingSeek = false;
        UpdatePlayIcon();
        UpdateTimeText();
        SyncFrame();
        LayoutCanvas();
        Canvas.Invalidate();
        StartClock();
        await PrepareSoundAsync(card);
        return true;
    }

    /// <summary>
    /// 打开预览时问一次这张卡的特效挂着什么音效，能不能放。
    /// 只有能放才显示那枚开关，而且每次打开都是关着的（用户 2026-11-21 定）。
    /// </summary>
    private async Task PrepareSoundAsync(int card)
    {
        StopSound();
        _seLabel = "";
        SoundToggle.IsChecked = false;
        SoundIcon.Glyph = MuteGlyph;
        SoundToggle.Visibility = Visibility.Collapsed;
        if (_kind != "runfx" || App.Engine is not { } engine) return;

        try
        {
            var info = await engine.CallAsync("preview.sound", new { card });
            if (info.ValueKind != JsonValueKind.Object) return;
            if (!info.TryGetProperty("playable", out var okEl) || !okEl.GetBoolean()) return;
            _seLabel = info.TryGetProperty("label", out var lbEl) ? (lbEl.GetString() ?? "") : "";
            if (_seLabel.Length == 0) return;
            SoundToggle.Visibility = Visibility.Visible;
            ToolTipService.SetToolTip(SoundToggle, "音效，默认关");
        }
        catch
        {
            // 问不到就不显示开关，预览本身照常播
        }
    }

    private void StartClock()
    {
        _clock.Restart();
        _lastTick = TimeSpan.Zero;
        _timer.Start();
    }

    private static SKBitmap? LoadAsset(string rel)
    {
        try
        {
            var path = Path.Combine(AppContext.BaseDirectory, rel);
            return File.Exists(path) ? SKBitmap.Decode(path) : null;
        }
        catch
        {
            return null;
        }
    }

    private void OnLoaded(object sender, RoutedEventArgs e) => LayoutCanvas();

    private void OnUnloaded(object sender, RoutedEventArgs e) => Close();

    /// <summary>
    /// 关掉播放：停时钟、释放帧图。浮层只是把可见性收起来，控件还留在树里，
    /// 所以不能靠 Unloaded 来收尾，调用方要显式调这个。
    /// </summary>
    public void Close()
    {
        _timer.Stop();
        _playing = false;
        _lastFrame = -1;
        StopSound();
        SoundToggle.Visibility = Visibility.Collapsed;
        SoundToggle.IsChecked = false;
        _seLabel = "";
        ClearFrames();
        _frameCount = 0;
        _frameDir = "";
        _field?.Dispose();
        _field = null;
    }

    /// <summary>
    /// 取第 idx 帧。没解过就现解，并把离当前帧太远的位图释放掉。
    /// 只留当前帧前后 ±KeepFrames 张，内存跟帧数脱钩。
    /// </summary>
    private SKBitmap? EnsureFrame(int idx)
    {
        if (idx < 0 || idx >= _frameCount || _frameDir.Length == 0) return null;
        lock (_cacheGate)
        {
            if (_cache.TryGetValue(idx, out var hit)) return hit;
            var path = Path.Combine(_frameDir, $"{idx:000}.webp");
            if (!File.Exists(path)) return null;
            var bmp = SKBitmap.Decode(path);
            if (bmp is null) return null;
            _cache[idx] = bmp;

            List<int>? drop = null;
            foreach (var k in _cache.Keys)
                if (Math.Abs(k - idx) > KeepFrames) (drop ??= new List<int>()).Add(k);
            if (drop is not null)
            {
                foreach (var k in drop)
                {
                    _cache[k].Dispose();
                    _cache.Remove(k);
                }
            }
            return bmp;
        }
    }

    private void ClearFrames()
    {
        lock (_cacheGate)
        {
            foreach (var b in _cache.Values) b.Dispose();
            _cache.Clear();
        }
    }

    /// <summary>画布铺不满舞台时居中留边，永远保持 16:9。</summary>
    private void LayoutCanvas()
    {
        double aw = Stage.ActualWidth, ah = Stage.ActualHeight;
        if (aw <= 0 || ah <= 0) return;
        double w = Math.Min(aw, ah * Aspect);
        double h = w / Aspect;
        if (Math.Abs(Canvas.Width - w) < 0.5 && Math.Abs(Canvas.Height - h) < 0.5) return;
        Canvas.Width = w;
        Canvas.Height = h;
        Canvas.Invalidate();
    }

    private void Stage_SizeChanged(object sender, SizeChangedEventArgs e) => LayoutCanvas();

    private void Advance()
    {
        if (!_playing || _frameCount == 0) return;
        var now = _clock.Elapsed;
        double dt = (now - _lastTick).TotalSeconds;
        _lastTick = now;
        if (dt <= 0 || dt > 0.25) dt = 1.0 / 60;

        _time += dt * _speed;
        if (_time >= _duration)
        {
            if (_loop)
            {
                _time %= _duration;
                SoundFollow(seek: true);
            }
            else
            {
                _time = _duration;
                _playing = false;
                _timer.Stop();
                UpdatePlayIcon();
                SoundFollow(seek: false);
            }
        }
        else
        {
            // 只对齐播放/暂停，不每帧 seek，否则会一直重新定位、听着卡
            SoundFollow(seek: false);
        }
        _syncingSeek = true;
        Seek.Value = _time;
        _syncingSeek = false;
        UpdateTimeText();
        SyncFrame();
        Canvas.Invalidate();
    }

    private void OnPaint(object sender, SKPaintSurfaceEventArgs e)
    {
        var canvas = e.Surface.Canvas;
        int w = e.Info.Width, h = e.Info.Height;
        canvas.Clear(_blackBg ? new SKColor(0, 0, 0) : new SKColor(0xFA, 0xFB, 0xFC));

        if (!_blackBg && _field is { } field)
        {
            // 线稿压到 30%：压得太淡看不出格子，太浓会抢主体
            using var paint = new SKPaint { Color = SKColors.White.WithAlpha(77) };
            canvas.DrawBitmap(field, Fit(field.Width, field.Height, w, h), paint);
        }
        if (_frameCount == 0) return;
        // 按需解码：画到哪解到哪，解不出来就这一帧不画
        if (EnsureFrame(FrameIndex()) is not { } bmp) return;
        if (_kind == "runfx")
        {
            // 特效：占屏**宽** = 画面大小，高度按帧的宽高比反推。
            // 2026-11-22 用 1024×1024 正方形素材在 #22159 上定死：画面大小 0.5 时，
            // 游戏里白框 640×639px @1440p，占屏宽 50.0%、占屏高 88.8%。
            // 之前按「占屏高」画，正方形素材会小 1.78 倍，16:9 素材因为两种口径数值
            // 相同一直没暴露（22159 那两档 16:9 标定就分不出来）。
            // 注意这是「帧宽」的占比，帧四周的透明留白也算在内；引擎把界面上的
            // 「画面高度」换算成这个数值时用的是可见内容的包围盒（2026-11-22 修正）。
            double fxW = w * _size;
            double fxH = fxW * bmp.Height / Math.Max(1, bmp.Width);
            double cx = w / 2.0 + _offXPct / 100.0 * w;
            double cy = h / 2.0 - _offYPct / 100.0 * h;
            using var img2 = new SKPaint { IsAntialias = true };
            bool rotated = Math.Abs(_rot) > 0.01;
            if (rotated)
            {
                canvas.Save();
                canvas.RotateDegrees((float)-_rot, (float)cx, (float)cy);
            }
            canvas.DrawBitmap(bmp, new SKRect((float)(cx - fxW / 2), (float)(cy - fxH / 2),
                                             (float)(cx + fxW / 2), (float)(cy + fxH / 2)), img2);
            if (rotated) canvas.Restore();
            return;
        }

        double scale = ScaleAt(_time);
        double unitPx = h / UnitsToPixels;                 // 1 骨骼单位 = 多少画布像素
        double drawH = _sizeUnits * _zoom * unitPx * scale;
        double drawW = drawH * bmp.Width / Math.Max(1, bmp.Height);
        // 位移在骨架里是根骨骼的位置（骨骼单位），不吃曲线缩放与放大倍数，
        // 所以这里按 size_units 折算，而不是按已经缩放过的 drawH。
        // 1.0 = 一个角色高度（角色高度 1.00 时正好一屏）。
        double posX = w / 2.0 + _cutOffX * _sizeUnits * unitPx;
        double posY = h / 2.0 - _cutOffY * _sizeUnits * unitPx;
        var rect = new SKRect((float)(posX - drawW / 2), (float)(posY - drawH / 2),
                              (float)(posX + drawW / 2), (float)(posY + drawH / 2));
        using var img = new SKPaint { IsAntialias = true };
        bool spun = Math.Abs(_cutRot) > 0.01;
        if (spun)
        {
            canvas.Save();
            canvas.RotateDegrees((float)-_cutRot, (float)posX, (float)posY);
        }
        canvas.DrawBitmap(bmp, rect, img);
        if (spun) canvas.Restore();

        // 召唤演出里游戏会在下方盖两块界面：卡名条与攻守条。这里只画灰色占位块，
        // 位置按 1280×720 素材上量出来的比例，跟着画布缩放，不跟角色大小走
        // （它们是界面元素，贴屏幕不贴角色）。画在角色之后，所以永远在最上层。
        // 纯黑背景不画：那两块是给场地线稿做位置参考的，黑底上只剩两根灰条，看着像残留物。
        if (!_blackBg && _time <= PlateSeconds)
        {
            using var band = new SKPaint { IsAntialias = false };
            band.Color = new SKColor(0x80, 0x80, 0x80, 128);   // 卡名条：半透明
            canvas.DrawRect(Box(NameBox, w, h), band);
            band.Color = new SKColor(0x80, 0x80, 0x80, 255);   // 攻守条：不透明
            canvas.DrawRect(Box(AtkBox, w, h), band);
        }
    }

    // 两块界面框的显示时长：实测 1.6 秒动画里 1.45 秒、2 秒动画里 1.34 秒，
    // 都在 1.4 秒上下，不随动画总时长放大（2026-11-21 用户两支录屏）。
    private const double PlateSeconds = 1.4;

    // 左、上、右、下，都是占画布的比例
    private static readonly double[] NameBox = { 0.08, 0.780, 0.92, 0.880 };
    private static readonly double[] AtkBox = { 0.30, 0.900, 0.70, 0.955 };

    private static SKRect Box(double[] r, int w, int h)
        => new SKRect((float)(r[0] * w), (float)(r[1] * h), (float)(r[2] * w), (float)(r[3] * h));

    /// <summary>等比铺满（cover），线稿按 16:9 画进画布</summary>
    private static SKRect Fit(float sw, float sh, int w, int h)
    {
        float k = Math.Max(w / sw, h / sh);
        float dw = sw * k, dh = sh * k;
        return new SKRect((w - dw) / 2, (h - dh) / 2, (w + dw) / 2, (h + dh) / 2);
    }

    /// <summary>曲线是按「比例帧号」写的，时间先换成比例再分段插值</summary>
    private double ScaleAt(double t)
    {
        if (_curveFrames.Length == 0) return 1.0;
        double f = _duration <= 0 ? 0 : t / _duration * _curveLast;
        if (f <= _curveFrames[0]) return _curveScales[0];
        for (int i = 1; i < _curveFrames.Length; i++)
        {
            if (f <= _curveFrames[i])
            {
                double span = _curveFrames[i] - _curveFrames[i - 1];
                if (span <= 0) return _curveScales[i];
                double k = (f - _curveFrames[i - 1]) / span;
                return _curveScales[i - 1] + (_curveScales[i] - _curveScales[i - 1]) * k;
            }
        }
        return _curveScales[^1];
    }

    private void UpdatePlayIcon()
        => PlayIcon.Glyph = _playing ? "\uE769" : "\uE768";

    private void UpdateTimeText()
        => TimeText.Text = $"{_time:0.00} / {_duration:0.00}";

    /// <summary>当前该显示第几张（0 起）。播放、拖动、逐帧改的都是 _time，换算只此一处。</summary>
    private int FrameIndex()
    {
        if (_frameCount == 0) return 0;
        int idx = (int)Math.Round(_time / _duration * (_frameCount - 1));
        return Math.Clamp(idx, 0, _frameCount - 1);
    }

    /// <summary>帧号变了才通知外面。循环回到第 1 帧、播完停在末帧都算变化。</summary>
    private void SyncFrame()
    {
        if (_frameCount == 0) return;
        int cur = FrameIndex() + 1;
        if (cur == _lastFrame) return;
        _lastFrame = cur;
        FrameChanged?.Invoke(cur, _frameCount);
    }

    private void Play_Click(object sender, RoutedEventArgs e) => TogglePlay();

    /// <summary>空格与按钮共用：暂停时若停在末帧，从头开始。</summary>
    public void TogglePlay()
    {
        if (_frameCount == 0) return;
        if (!_playing && _time >= _duration - 1e-6)
        {
            _time = 0;
            _syncingSeek = true;
            Seek.Value = 0;
            _syncingSeek = false;
            UpdateTimeText();
            SyncFrame();
        }
        _playing = !_playing;
        if (_playing) StartClock();
        else _timer.Stop();
        UpdatePlayIcon();
        SoundFollow(seek: true);
        Canvas.Invalidate();
    }

    private void PrevFrame_Click(object sender, RoutedEventArgs e) => StepFrame(-1);

    private void NextFrame_Click(object sender, RoutedEventArgs e) => StepFrame(1);

    /// <summary>逐帧步进：先暂停，再按「一帧 = 时长/帧间隔」往前挪。</summary>
    private void StepFrame(int dir)
    {
        if (_frameCount == 0) return;
        if (_playing)
        {
            _playing = false;
            _timer.Stop();
            UpdatePlayIcon();
        }
        double step = _duration / Math.Max(1, _frameCount - 1);
        _time = Math.Clamp(_time + dir * step, 0, _duration);
        _syncingSeek = true;
        Seek.Value = _time;
        _syncingSeek = false;
        UpdateTimeText();
        SyncFrame();
        SoundFollow(seek: true);
        Canvas.Invalidate();
    }

    private void Speed_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not MenuFlyoutItem item) return;
        var text = item.Tag?.ToString() ?? "1";
        _speed = double.TryParse(text, System.Globalization.NumberStyles.Float,
            System.Globalization.CultureInfo.InvariantCulture, out var v) && v > 0 ? v : 1;
        SpeedText.Text = $"{_speed:0.#}×";
        // 跟随变速：音调会跟着变高变低，预览里够用（2026-11-21 用户选的）
        if (_soundPlayer is not null)
        {
            try { _soundPlayer.PlaybackSession.PlaybackRate = _speed; } catch { }
        }
    }

    private void Seek_ValueChanged(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (!_loaded || _syncingSeek) return;
        _time = Math.Clamp(e.NewValue, 0, _duration);
        UpdateTimeText();
        SyncFrame();
        SoundFollow(seek: true);
        Canvas.Invalidate();
    }

    private void Loop_Click(object sender, RoutedEventArgs e)
    {
        _loop = LoopToggle.IsChecked == true;
    }

    // ---------------------------------------------------------------- 音效

    /// <summary>
    /// 音效开关。默认关，点开时才去把音效解成 wav（不想为没开的声音付解码成本）。
    /// </summary>
    private async void Sound_Click(object sender, RoutedEventArgs e)
    {
        bool on = SoundToggle.IsChecked == true;
        if (!on)
        {
            _soundOn = false;
            StopSound();
            SoundIcon.Glyph = MuteGlyph;
            return;
        }

        _soundOn = true;
        if (!await EnsureSoundAsync())
        {
            // 解不出来就退回关着的状态，别让按钮亮着却听不见
            _soundOn = false;
            SoundToggle.IsChecked = false;
            SoundIcon.Glyph = MuteGlyph;
            return;
        }
        SoundIcon.Glyph = SoundGlyph;
        SoundFollow(seek: true);
    }

    /// <summary>把音效解成临时 wav 并备好播放器。已经解过就直接复用。</summary>
    private async Task<bool> EnsureSoundAsync()
    {
        if (_soundPlayer is not null) return true;
        if (_seLabel.Length == 0 || App.Engine is not { } engine) return false;
        try
        {
            var data = await engine.CallAsync("sound.preview", new Dictionary<string, object?>
            {
                ["label"] = _seLabel,
            });
            var path = data.ValueKind == JsonValueKind.Object &&
                       data.TryGetProperty("path", out var pEl) ? (pEl.GetString() ?? "") : "";
            if (path.Length == 0 || !File.Exists(path)) return false;

            // 走 StorageFile 而不是 file:// URI：非打包桌面应用里这条路更稳
            var file = await StorageFile.GetFileFromPathAsync(path);
            var player = new MediaPlayer { Source = MediaSource.CreateFromStorageFile(file) };
            // 源还没打开时设速率可能抛，单独兜一下，别连累「音效可用」这件事
            try { player.PlaybackSession.PlaybackRate = _speed; } catch { }
            _soundPlayer = player;
            return true;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>
    /// 让音效跟上画面。seek=true 时顺带定位（拖动、逐帧、循环回到开头），
    /// 平时只对齐播放/暂停，免得每帧重新定位发卡。
    /// </summary>
    private void SoundFollow(bool seek)
    {
        if (!_soundOn || _soundPlayer is null) return;
        double t = _time - SeDelay;
        try
        {
            if (seek)
                _soundPlayer.PlaybackSession.Position = TimeSpan.FromSeconds(Math.Max(0, t));
            if (t <= 0)
            {
                _soundPlayer.Pause();
                return;
            }
            var state = _soundPlayer.PlaybackSession.PlaybackState;
            if (_playing)
            {
                if (state != MediaPlaybackState.Playing) _soundPlayer.Play();
            }
            else if (state == MediaPlaybackState.Playing)
            {
                _soundPlayer.Pause();
            }
        }
        catch
        {
            // 源还没打开时定位会抛，下一次 tick 再对齐就行
        }
    }

    private void StopSound()
    {
        if (_soundPlayer is null) return;
        try
        {
            _soundPlayer.Pause();
            _soundPlayer.Dispose();
        }
        catch
        {
        }
        _soundPlayer = null;
    }

    private void Background_Click(object sender, RoutedEventArgs e)
    {
        _blackBg = !_blackBg;
        BgIcon.Glyph = _blackBg ? BlackGlyph : FieldGlyph;
        Canvas.Invalidate();
    }
}
