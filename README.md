# Auto Review (for Codex)

中文 | [English version](README.en.md)

顾名思义，在修改文件相关的 prompt 中使用这个 skill 后，Codex 会在**完成修改后**主动 review `本次任务的修改`。如发现问题，会在原任务范围内做**循环repair + review**（最多两轮）。

## 使用方式

为避免不必要的review，本skill不会自动触发，必须**显式**启用。
作为 Auto Workflow Plugin 的 bundled skill，规范入口是
`$auto-workflow:auto-review`，例如：

```text
$auto-workflow:auto-review 修复xxx问题
```

```text
$auto-workflow:auto-review 新增xxx功能
```

也可以只输入：

```text
$auto-workflow:auto-review
```

这个用法会优先review当前会话最近的实质修改任务。

为兼容已有 prompt 和 prepared workflow，bundled hook 仍接受旧入口
`$auto-review`；新文档和新 workflow 应使用完整名。

skill已适配 `/plan` 和 `/goal` 。

在**plan**模式中使用此skill，不会立即触发，而是在实现plan之后才触发。

在**goal**模式下使用，则会在任务完成（被标记为 `complete`）后触发。


## 前置要求

- 已安装 Codex。
- 已安装 Python 3.10 或更新版本。


## 安装

本节保留独立安装方式；独立安装后的 Skill 名仍是 `$auto-review`。团队通过
Auto Workflow Plugin 安装时使用前文的完整名 `$auto-workflow:auto-review`。

### MacOS / Linux

在工程目录中直接执行安装脚本：

```bash
./install.sh
```

MacOS / Linux默认为 symlink 模式（不复制文件，只创建链接（快捷方式）），如果想直接复制文件，可使用：

```bash
./install.sh --mode copy
```

### Windows

```powershell
.\install.ps1
```

Windows默认为 copy 模式（直接复制文件），如果要使用 symlink 模式，需要启用 Windows Developer Mode，或以管理员身份运行 PowerShell：

```powershell
.\install.ps1 -Mode symlink
```

安装或更新后，需要完整退出并重启 Codex。仅新建或 fork session 不会生效。

## 卸载

MacOS / Linux：

```bash
cd /path/to/auto-review
./uninstall.sh
```

Windows PowerShell：

```powershell
cd C:\path\to\auto-review
.\uninstall.ps1
```

## 实现原理

利用Codex提供的hook机制监听任务的`Stop`信号，然后自动发起review：
```text
review本次修改，检查是否存在遗漏，逻辑错误等问题
```
如果没有发现问题，review会直接结束。如果问题能在原任务范围内解决，会自动提交：
```text
修复这些问题然后重新做一次review
```
