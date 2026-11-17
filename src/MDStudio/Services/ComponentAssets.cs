using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace MDStudio.Services;

/// <summary>一个可选组件（AI 抠图模型、超分组件、Spine 转换器）。</summary>
public sealed record AssetItem(string Key, string Label, long Size);

/// <summary>
/// 组件缺失时的统一处理：问后端这次操作要哪些组件、缺哪几个，
/// 缺了就提示并跳到设置页对应位置。
///
/// 这些组件只影响个别功能，不该挡住整个程序：所以没有启动门禁，
/// 全靠在动作开始前问一句。
/// </summary>
internal static class ComponentAssets
{
    /// <summary>
    /// 这次操作缺哪些组件。kind：spine / cutin.video / runfx.video。
    ///
    /// 返回 null 表示「没问出来」，跟「问了，不缺」是两回事：
    /// 2026-11-16 那天预检被任务队列挤掉两次，旧代码按「不缺」放行，
    /// 结果缺着抠像模型直接跑起来了。所以现在问不到就不让开始。
    /// </summary>
    public static async Task<List<AssetItem>?> MissingAsync(string kind, string? source = null,
                                                           bool sharp = false)
    {
        if (App.Engine is not { } engine) return null;

        JsonElement r;
        try
        {
            r = await engine.CallAsync("assets.needs", new { kind, source, sharp });
        }
        catch (Exception ex)
        {
            // 引擎忙着别的任务时这条会被拒（Busy）。等一下再问一次，
            // 别把「没问到」当成「不缺组件」，否则缺组件时会直接冲到正式流程里报错。
            App.AddLog("WARN", $"组件预检第一次失败，正在重试：{ex.Message}");
            try
            {
                await Task.Delay(500);
                r = await engine.CallAsync("assets.needs", new { kind, source, sharp });
            }
            catch (Exception ex2)
            {
                App.AddLog("WARN", $"组件预检失败，本次不放行：{ex2.Message}");
                return null;
            }
        }

        if (r.ValueKind != JsonValueKind.Object ||
            !r.TryGetProperty("missing", out var arr) || arr.ValueKind != JsonValueKind.Array)
            return null;

        var list = new List<AssetItem>();
        foreach (var a in arr.EnumerateArray())
            list.Add(new AssetItem(Str(a, "key"), Str(a, "label"), Num(a, "size")));
        return list;
    }

    /// <summary>
    /// 缺组件就弹提示并导航到设置页，返回 false 让调用方中断当前操作；
    /// 组件齐了直接返回 true。
    /// </summary>
    public static async Task<bool> EnsureAsync(XamlRoot? root, string kind,
                                               string? source = null, bool sharp = false)
    {
        var missing = await MissingAsync(kind, source, sharp);
        if (missing is null)
        {
            // 问不到就别开始：宁可让用户等两秒重试，也不要在缺组件的情况下跑到一半
            await Dialogs.InfoAsync(root, "组件状态没查成",
                "引擎正忙，这一下没问到这次操作需要哪些组件。",
                danger: false, null, null,
                "等几秒，等别的动作跑完再点一次就好。没有开始任何写入。");
            return false;
        }
        if (missing.Count == 0) return true;
        await PromptAsync(root, missing);
        return false;
    }

    /// <summary>弹出「需要下载组件」，用户点「去下载」就跳到设置页。</summary>
    public static async Task PromptAsync(XamlRoot? root, IReadOnlyList<AssetItem> missing)
    {
        if (root is null || missing.Count == 0) return;

        var names = string.Join("、", missing.Select(m => m.Label));
        long total = missing.Sum(m => m.Size);
        // 主按钮有实际动作（跳设置页），用 ConfirmAsync 的返回值接，
        // 不需要额外一套对话框。
        var go = await Dialogs.ConfirmAsync(root, "需要先下载组件",
            $"这个功能要用到{names}，现在还没下载（共 {total / 1024.0 / 1024.0:F0} MB）。",
            "去下载", null, null,
            "装好之后回到这一页，重新点一次就行，不用重开软件。");
        if (!go) return;
        (App.MainWindow as MainWindow)?.GoToSettings(missing[0].Key);
    }

    private static string Str(JsonElement e, string name) =>
        e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? "" : "";

    private static long Num(JsonElement e, string name) =>
        e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number
            ? v.GetInt64() : 0;
}
