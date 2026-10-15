import { useEffect, useState } from 'react';
import type { LocalClaudeService } from '../services/localClaudeService';
import type { ClaudeOverview, ClaudeSwitchAction, ClaudeSwitchOperation } from '../types/localClaude';

const FINISHED = new Set(['complete', 'rolled_back', 'cancelled', 'abandoned']);
const AUTOMATIC_STEPS = new Set(['planned', 'copied', 'launching', 'registering']);
const COPY_FAILURES: Record<string, string> = {
  active: '相关进程重新出现，或无法确认其归属；文件交接已停止',
  changing: '文件在交接期间发生变化，或未完整写入',
  conflict: '目标出现了冲突文件，没有覆盖它',
  path: '文件路径或权限检查未通过',
  io_error: '文件操作失败；详细原始错误未写入回执',
  unexpected: '文件交接异常中止；没有将原始错误写入回执',
  copy_interrupted: '文件交接中断；旧回执没有记录具体原因',
};

export function LocalClaudeSwitch({ service, overview, readonly, externalBusy, onUpdated, onBusyChange }: {
  service: LocalClaudeService; overview: ClaudeOverview; readonly: boolean;
  externalBusy: boolean;
  onUpdated: (overview: ClaudeOverview) => void; onBusyChange: (busy: boolean) => void;
}) {
  const [source, setSource] = useState('');
  const [target, setTarget] = useState('');
  const [operations, setOperations] = useState<ClaudeSwitchOperation[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [quitConfirmed, setQuitConfirmed] = useState(false);
  const [checks, setChecks] = useState([false, false, false, false]);
  const active = operations.find((op) => !FINISHED.has(op.stage));
  const recent = active ?? operations[0];
  const label = (id: string) => {
    const profile = overview.profiles.find((p) => p.id === id);
    if (profile) return profile.label;
    const archived = overview.archivedProfiles?.find((p) => p.id === id);
    return archived ? `${archived.label}（已归档）` : '未知配置';
  };

  useEffect(() => {
    if (!overview.profiles.some((p) => p.id === source)) setSource(overview.selectedProfileId ?? overview.profiles[0]?.id ?? '');
    if (target && (target === source || !overview.profiles.some((p) => p.id === target))) setTarget('');
  }, [source, target, overview]);
  useEffect(() => {
    if (!readonly) void service.switches().then(setOperations).catch(() => setError('暂时无法读取切换回执。'));
  }, [readonly, service]);

  const keep = (next: ClaudeSwitchOperation) => {
    setOperations((old) => [next, ...old.filter((op) => op.id !== next.id)]);
    if (next.stage === 'complete') {
      setSource(next.targetId);
      setTarget('');
    }
  };
  const drive = async (first: ClaudeSwitchOperation, quitting = false) => {
    let next = first;
    let desktopWaits = 0;
    let exitWaits = 0;
    do {
      next = await service.switchAction(next.id, 'advance'); keep(next);
      if (next.stage === 'awaiting_verification') {
        next = await service.switchAction(next.id, 'verify_auto'); keep(next);
      }
      if (next.stage === 'waiting_desktop' && desktopWaits++ < 20) {
        await new Promise((resolve) => window.setTimeout(resolve, 500));
        continue;
      }
      if (quitting && next.stage === 'waiting_exit' && exitWaits++ < 20) {
        await new Promise((resolve) => window.setTimeout(resolve, 500));
        continue;
      }
      if (!AUTOMATIC_STEPS.has(next.stage)) break;
    } while (true);
  };
  const run = async (task: () => Promise<void>) => {
    if (busy || externalBusy) return;
    setBusy(true); onBusyChange(true); setError('');
    try { await task(); onUpdated(await service.list()); }
    catch { setError('操作未完成。请重新读取回执后继续，已保存的阶段不会丢失。'); }
    finally { setBusy(false); onBusyChange(false); }
  };
  const act = (action: ClaudeSwitchAction, confirmed = false, continueAfter = false) => {
    if (!active) return;
    void run(async () => {
      const next = await service.switchAction(active.id, action, confirmed); keep(next);
      if (continueAfter && !next.lastErrorCode) await drive(next);
    });
  };
  const locked = busy || externalBusy;

  return <section className="lcl-sessions" aria-label="Claude Desktop 账号切换">
    <header className="lcl-sessions-header"><h2>切换 Desktop 账号</h2><button className="btn btn-secondary" disabled={readonly || locked} onClick={() => void run(async () => setOperations(await service.switches()))}>读取回执</button></header>
    <p className="lcl-quiet-note">保全已落盘的 CLI 历史，通过官方入口登记到目标 Desktop。运行中的任务须先结束；不迁移执行中的内存、云聊天或任意完整工作环境。</p>
    {error && <p className="lcl-inline-error" role="alert">{error}</p>}
    {!active && <div className="lcl-session-selectors">
      <label>当前使用的账号<select value={source} disabled={locked || readonly} onChange={(event) => setSource(event.target.value)}>{overview.profiles.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}</select></label>
      <label>切换到<select value={target} disabled={locked || readonly} onChange={(event) => setTarget(event.target.value)}><option value="">选择目标账号</option>{overview.profiles.filter((p) => p.id !== source).map((p) => <option key={p.id} value={p.id} disabled={p.health?.status === 'account_disabled'}>{p.label}{p.health?.status === 'account_disabled' ? '（官方停用）' : ''}</option>)}</select></label>
      <button className="btn btn-primary" disabled={locked || readonly || !source || !target || source === target} onClick={() => void run(async () => { let op = await service.prepareSwitch(source, target); keep(op); setChecks([false, false, false, false]); setQuitConfirmed(false); op = await service.switchAction(op.id, 'request_quit', true); keep(op); if (!op.lastErrorCode) await drive(op, true); })}>切换（先请求正常退出当前 Claude）</button>
    </div>}
    {recent && <div className="lcl-snapshot-result"><div>
      <strong>{label(recent.sourceId)} → {label(recent.targetId)}</strong>
      <p role="status">{recent.message}</p>
      {recent.verificationMode === 'official_local_metadata' && <p>已自动核对近期官方可用证据、目标身份及官方会话元数据。尚未发送新模型请求，也未独立验证视觉布局或模型设置；本流程没有改写模型、effort 或 Auto。</p>}
      <span>{recent.sessionCount} 条本地历史，已登记 {recent.registeredCount} 条；保存于 {new Date(recent.updatedAt * 1000).toLocaleString()}</span>
      {recent.lastErrorCode && <p className="lcl-inline-error">此阶段未通过：{COPY_FAILURES[recent.lastErrorCode] ?? recent.lastErrorCode}。请处理该条件后继续；不会自动重试登录或切换账号。</p>}
      <details><summary>切换与备份回执</summary><code>{recent.id}</code><span>源备份：{recent.sourceBackupId ?? '尚未创建'}</span><span>目标备份：{recent.targetBackupId ?? '尚未创建'}</span><span>文件交接：{recent.handoffReceiptId ?? '尚未复制'}</span></details>
    </div></div>}
    {active && <>
      {['planned', 'waiting_exit', 'rollback_waiting_exit'].includes(active.stage) && <div>
        <label className="lcl-checkbox"><input type="checkbox" checked={quitConfirmed} onChange={(event) => setQuitConfirmed(event.target.checked)} disabled={locked} />我已结束任务，允许请求正常退出。若官方仍提示有任务，由我决定如何处理。</label>
        <button className="btn btn-secondary" disabled={locked || !quitConfirmed} onClick={() => act('request_quit', true, active.stage !== 'rollback_waiting_exit')}>请求正常退出</button>
      </div>}
      {active.stage === 'waiting_login' && <p>请在目标账号卡片完成官方 CLI 登录并退出登录终端，再继续。此时会在账号空闲时复核登录及官方用量服务；Desktop 若要求登录，仍由其官方界面完成。</p>}
      {active.stage === 'waiting_desktop' && <button className="btn btn-secondary" disabled={locked} onClick={() => act('retry_launch', true, true)}>目标窗口未启动时，重新请求启动</button>}
      {active.stage === 'registration_uncertain' && <div className="lcl-actions">
        <button className="btn btn-secondary" disabled={locked} onClick={() => act('ack_registration', true, true)}>已在界面看到该条，继续</button>
        <button className="btn btn-secondary" disabled={locked} onClick={() => act('retry_registration', true, true)}>明确重试上一条登记</button>
      </div>}
      {['awaiting_verification', 'confirming'].includes(active.stage) && <div>
        {['已在官方界面确认目标账号身份', '全部本地项目和历史都可见', '已打开原工作会话并核对历史', '模型、effort、Auto 保持期望设置，没有扩大权限'].map((text, index) => <label className="lcl-checkbox" key={text}><input type="checkbox" checked={checks[index]} onChange={(event) => setChecks((old) => old.map((value, i) => i === index ? event.target.checked : value))} disabled={locked} />{text}</label>)}
        <button className="btn btn-primary" disabled={locked || !checks.every(Boolean)} onClick={() => act('confirm', true)}>确认验收并设为 Cockpit 默认</button>
      </div>}
      <div className="lcl-actions">
        {!['awaiting_verification', 'confirming', 'registration_uncertain', 'copy_incomplete', 'rollback_waiting_exit'].includes(active.stage) && <button className="btn btn-secondary" disabled={locked} onClick={() => void run(() => drive(active))}>继续已保存的阶段</button>}
        {active.rollbackAvailable && <button className="btn btn-secondary" disabled={locked} onClick={() => act('rollback', true)}>回退本次未再变化的文件</button>}
        {active.canCancel && <button className="btn btn-secondary" disabled={locked} onClick={() => act('cancel')}>取消切换</button>}
        {!active.canCancel && <button className="btn btn-secondary" disabled={locked} onClick={() => act('abandon', true)}>保留全部文件，结束本次流程</button>}
      </div>
    </>}
    <p className="lcl-quiet-note">默认账号只影响从 Cockpit 启动；终端或其他应用中的启动方式沿用各自配置。</p>
  </section>;
}
