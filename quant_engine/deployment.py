"""部署环境的服务控制（供各管理命令复用）。

Windows 单机部署里，Web / Scheduler / nginx 由计划任务
``QuantEngine-*`` 拉起（经 ``deploy/windows/run_hidden.vbs`` 隐藏窗口运行）。
数据类管理命令（同步标的、同步 K 线等）改动数据前需要停服，改完再恢复。

为什么单独抽出来
----------------
``sync_symbols`` 最初把服务控制逻辑内联在命令里，后来踩了两个坑：
**探测输出乱码**导致识别不出运行中的服务、**``-File`` 不解析逗号**导致
恢复调用失败。这两个坑都与业务无关，但会在每个新命令里重演，
故抽成共享 mixin，避免"复制粘贴式复现"。

两个必须记住的约束（都已在这里处理好）
--------------------------------------
1. **只能按 ASCII 片段匹配子进程输出**：``services.ps1`` 的输出经 PowerShell
   写出时是系统 ANSI（中文 Windows = GBK），而 :func:`subprocess.run` 用
   ``encoding='utf-8'`` 解码，中文会变乱码。按 ``任务=Running`` 这类中文标记
   匹配**永远为假** → 运行中的服务被判为空 → 停服后不恢复，线上停摆。
   GBK 与 UTF-8 对 ASCII 兼容，故只匹配 ``Running``。
2. **``-Service`` 要传逗号串而非数组**：PowerShell 的 ``-File`` 调用方式
   不解析逗号，``powershell -File services.ps1 -Service Web,Scheduler``
   会把整个 ``"Web,Scheduler"`` 当成单个值。故这里统一拼成逗号串，
   目标脚本内部自行拆分。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# services.ps1 认识的服务名（用于校验探测结果、过滤无关行）
# ⚠️ 新增服务时**必须同步这里**，否则探测会把它当"不认识的行"而漏掉，
# 导致该服务不被纳入"同步前在运行"的判断。
KNOWN_SERVICES = ('Web', 'Scheduler', 'MCP', 'Nginx')


class ServiceControlMixin:
    """给管理命令提供"停服 → 改数据 → 恢复服务"的能力。

    使用方式::

        class Command(ServiceControlMixin, BaseCommand):
            ...
            was_running = self.stop_services()   # None/[]/[名称]
            ...
            self.restore_services(was_running)
    """

    # ------------------------------------------------------------------ 定位
    def services_script(self):
        """返回 ``deploy/windows/services.ps1`` 路径；非 Windows 或未安装则 None。"""
        if sys.platform != 'win32':
            return None
        # <repo>/quant_engine/deployment.py -> 仓库根是 parents[1]
        # （命令文件在 apps/<app>/management/commands/ 下，深度不同，别照抄）
        root = Path(__file__).resolve().parents[1]
        path = root / 'deploy' / 'windows' / 'services.ps1'
        return path if path.exists() else None

    # ------------------------------------------------------------------ 探测
    def probe_running(self):
        """同步前记录哪些服务在运行。

        返回**三态**：

        * ``None`` —— 探测**失效**（脚本缺失 / 调用失败 / 输出无法识别）；
        * ``[]``   —— 探测有效，确实没有服务在运行；
        * ``[名称]`` —— 探测有效，这些服务在运行。

        三态区分很重要：``None`` 与 ``[]`` 在恢复阶段会走向不同分支，
        混为一谈会导致「本来没人跑」被当成「探测失效」而乱启动服务。
        """
        script = self.services_script()
        if script is None:
            return None
        try:
            proc = subprocess.run(  # noqa: S603
                ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                 '-File', str(script), '-Action', 'status'],
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=120, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None

        running, seen = [], set()
        for line in (proc.stdout or '').splitlines():
            parts = line.split()
            if not parts:
                continue
            name = parts[0].strip()
            if name not in KNOWN_SERVICES:
                continue
            seen.add(name)
            # 形如：  Web        ???=Running   ??=2 ???? 8000 ???
            #中文在 utf-8 解码下是乱码，因此只认 ASCII 片段
            if 'Running' in line:
                running.append(name)
        # 一行服务状态都认不出来 → 输出格式与预期不符，判定为探测失效
        return running if seen else None

    # -------------------------------------------------------------- 停/启
    def stop_services(self):
        """停服，返回 :meth:`probe_running` 的三态结果。"""
        script = self.services_script()
        if script is None:
            self.stdout.write(self.style.WARNING(
                '未找到 deploy/windows/services.ps1（非 Windows 或未安装），'
                '请自行确保停服窗口内无并发读写。'))
            return None

        running = self.probe_running()
        self.stdout.write('')
        if running is None:
            self.stdout.write(self.style.WARNING(
                '  停服中（**探测失效，无法确定哪些服务在运行**）…'))
        else:
            self.stdout.write(
                f'  停服中（当前在运行：{", ".join(running) if running else "无"}）…')
        self.run_services_ps1(script, 'stop')
        return running

    def restore_services(self, was_running):
        """恢复服务。``was_running`` 为 :meth:`probe_running` 的返回值。"""
        if was_running is None:
            # 探测失效：无法区分"本来就没跑"和"没探测到"。
            # 「让线上停摆」的后果远重于「多启动一个服务」，故按保服务处理。
            self.stdout.write(self.style.WARNING(
                '未能确认哪些服务在运行（探测失效）。为避免线上停摆，将拉起全部服务。'))
            self.start_services(None)
        elif was_running:
            self.start_services(was_running)
        else:
            self.stdout.write('  同步前没有服务在运行，保持停止状态。')

    def start_services(self, running):
        """拉起服务。``running`` 为 None 表示"不知道哪些在跑"→ 全部拉起。"""
        script = self.services_script()
        if script is None:
            return
        if running is None:
            self.stdout.write('  拉起全部服务（兜底）…')
            self.run_services_ps1(script, 'start')
        elif not running:
            self.stdout.write('  同步前没有服务在运行，保持停止状态。')
        else:
            self.stdout.write(f'  恢复服务：{", ".join(running)}…')
            self.run_services_ps1(script, 'start', running)

    def run_services_ps1(self, script, action, services=None):
        """调用 services.ps1。

        ``services`` 列表会以**逗号串**传入——PowerShell 的 ``-File`` 方式
        不解析逗号，传数组会被当成单个值而遭 ValidateSet 拒绝。
        """
        args = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', str(script), '-Action', action]
        if services:
            args += ['-Service', ','.join(services)]
        try:
            proc = subprocess.run(  # noqa: S603
                args, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=300, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            self.stderr.write(self.style.WARNING(f'调用 services.ps1 失败：{exc}'))
            return
        if proc.returncode != 0:
            self.stderr.write(self.style.WARNING(
                f'services.ps1 -{action} 返回 {proc.returncode}，请确认服务状态：'))
            for line in ((proc.stderr or '') + '\n' + (proc.stdout or '')).splitlines()[-10:]:
                self.stderr.write(f'    {line}')
