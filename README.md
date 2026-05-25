# Auto Review (for Codex)

中文 | [English version](README.en.md)

顾名思义，当你在提交修改代码相关prompt时使用这个skill，它就会在codex**完成修改后**对`本轮修改的文件`**主动发起review**，如果发现问题，会**自动做修复**，修复后会**重做review**，直到检测不到问题为止。

## 使用方式

为避免不必要的review，本skill不会自动触发，必须**显式**启用。
跟其他skill一样，在prompt中加入`$auto-review` 即可，例如：

```text
$auto-review 修复xxx问题
```

```text
$auto-review 新增xxx功能
```

skill已适配 `/plan` 和 `/goal` 。

在**plan**模式中使用此skill，不会立即触发，而是在实现plan之后才触发。

在**goal**模式下使用，则会在任务完成（被标记为 `complete`）后触发。


## 前置要求

- 已安装 Codex。
- 已安装 Python 3.10 或更新版本。


## 安装

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

安装后重启 Codex。

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

利用Codex提供的hook机制监听任务的`Stop`信号， 然后自动提交一条prompt：
```text
review本次修改，检查是否存在遗漏，逻辑错误等问题
```
当这条prompt完成后，如果发现有问题，会继续提交prompt:
```text
请修复这些问题
```
这个过程会一直循环，直到review后找不到新的问题为止。

**注意：** 这个review的范围是使用skill那一轮修改所涉及到的文件，并不会额外对所有本地修改或当前分支做review。
