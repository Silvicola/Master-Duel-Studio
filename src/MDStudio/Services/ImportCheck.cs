using System.Text.Json;
using Microsoft.UI.Xaml;

namespace MDStudio.Services;

/// <summary>
/// 导入前的素材合法性检查。
///
/// 对话框的扩展名过滤拦不住人：在文件名框里粘贴完整路径时系统不看过滤器，
/// 把 .mp4 改成 .png 也能选。所以选完文件立刻问后端一句「这东西的内容真是那类文件吗」，
/// 不合格就弹窗说清楚，并把输入框留空。
///
/// 检查本身跑不起来（引擎没答上来）时放行，让正式流程去报错：
/// 不能因为一道辅助检查把正常操作拦死。
/// </summary>
internal static class ImportCheck
{
    /// <summary>want: image / video / frames / spine / game_dir。返回能否继续。</summary>
    public static async Task<bool> EnsureAsync(XamlRoot? root, string? path, string want)
    {
        if (string.IsNullOrWhiteSpace(path)) return false;
        if (App.Engine is not { } engine) return true;

        JsonElement r;
        try
        {
            r = await engine.CallAsync("probe.file", new { path, want });
        }
        catch (Exception ex)
        {
            App.AddLog("WARN", $"导入检查没跑成，先放行：{ex.Message}");
            return true;
        }

        if (r.ValueKind != JsonValueKind.Object ||
            !r.TryGetProperty("ok", out var okEl) ||
            okEl.ValueKind != JsonValueKind.False)
            return true;

        var reason = Str(r, "reason");
        var hint = Str(r, "hint");
        await Dialogs.InfoAsync(Dialogs.Root(root), Title(want),
            reason.Length > 0 ? reason : "这个文件用不了。",
            danger: true, cardName: null, cardMeta: null,
            note: hint.Length > 0 ? hint : null);
        return false;
    }

    /// <summary>标题只说「哪一类东西不对」，具体原因放正文，别再重复一遍。</summary>
    private static string Title(string want) => want switch
    {
        "image" => "这个图片用不了",
        "video" => "这个视频用不了",
        "frames" => "这个文件夹用不了",
        "spine" => "这个 Spine 素材用不了",
        "game_dir" => "这个目录不是游戏目录",
        "pack" => "这个整合包用不了",
        _ => "这个文件用不了",
    };

    private static string Str(JsonElement e, string name) =>
        e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? "" : "";
}
