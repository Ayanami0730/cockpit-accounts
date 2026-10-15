export interface WindowUsage {
  usedPercent: number;
  resetsAt: number | null;
}

export interface ClaudeUsage {
  fiveHour: WindowUsage | null;
  sevenDay: WindowUsage | null;
  extraUsage: {
    enabled: boolean;
    usedMinor: number | null;
    limitMinor: number | null;
    currency: string | null;
  } | null;
  observedAt: number | null;
  source: 'official_cli' | 'official_statusline' | 'desktop_cache' | 'none';
  error: string | null;
  errorCode?: string | null;
}

export interface ClaudeProfile {
  id: string;
  label: string;
  kind: 'current' | 'managed';
  configDir: string;
  desktopDir: string;
  createdAt: number;
  selected: boolean;
  collectorInstalled: boolean;
  autoRefresh: boolean;
  renewalDate: string | null;
  auth: {
    status: 'unknown' | 'signed_in' | 'signed_out' | 'error';
    email: string | null;
    plan: string | null;
    checkedAt: number | null;
    source?: 'cached_metadata' | 'official_cli' | null;
  };
  usage: ClaudeUsage;
  health?: ClaudeHealth;
}

export interface ClaudeHealth {
  status: 'unknown' | 'available' | 'auth_required' | 'account_disabled' | 'network_error' | 'unavailable';
  reason: string | null;
  source: 'none' | 'official_cli' | 'official_usage' | 'official_ui' | 'official_email';
  observedAt: number | null;
  lastSuccessfulAt: number | null;
  lastAttemptAt: number | null;
  lastAttemptOutcome: string | null;
}

export type ClaudeSwitchStage = 'planned' | 'waiting_exit' | 'waiting_login' | 'copying' | 'copy_incomplete' | 'copied' | 'launching' | 'waiting_desktop' | 'registering' | 'registration_uncertain' | 'awaiting_verification' | 'confirming' | 'rollback_waiting_exit' | 'failed' | 'complete' | 'rolled_back' | 'cancelled' | 'abandoned';
export type ClaudeSwitchAction = 'advance' | 'request_quit' | 'confirm' | 'verify_auto' | 'rollback' | 'cancel' | 'ack_registration' | 'retry_registration' | 'retry_launch' | 'abandon';
export interface ClaudeSwitchOperation {
  id: string;
  sourceId: string;
  targetId: string;
  stage: ClaudeSwitchStage;
  createdAt: number;
  updatedAt: number;
  sessionCount: number;
  registeredCount: number;
  handoffReceiptId: string | null;
  sourceBackupId: string | null;
  targetBackupId: string | null;
  lastErrorCode: string | null;
  verifiedAt: number | null;
  verificationMode: 'official_local_metadata' | 'user_verified_ui' | null;
  message: string;
  rollbackAvailable: boolean;
  canCancel: boolean;
}

export interface ClaudeOverview {
  profiles: ClaudeProfile[];
  archivedProfiles?: { id: string; label: string; kind: 'current' | 'managed'; archivedAt: number | null }[];
  selectedProfileId: string | null;
  desktopRunning: boolean;
  codeProcessCount: number;
  checkedAt: number;
  warning: string | null;
}

export interface ClaudePreflight {
  ok: boolean;
  checkedAt: number;
  cliPath: string | null;
  cliVersion: string | null;
  checks: { label: string; ok: boolean; detail: string }[];
}

export interface ClaudeActionResult { ok: boolean; message: string }
export type ClaudeAction = 'login_cli' | 'launch_cli' | 'launch_desktop' | 'open_usage' | 'install_collector';

export interface ClaudeSession {
  sessionId: string;
  label: string;
  cwd: string | null;
  projectKey: string;
  updatedAt: number;
  active: boolean;
  activity: string;
  parseStatus: string;
  stable: boolean;
  files: number;
  bytes: number;
}

export interface ClaudeSessionInventory {
  checkedAt: number;
  profileId: string;
  active: boolean;
  activityKnown: boolean;
  sessions: ClaudeSession[];
  summary: { files: number; bytes: number; jsonlFiles: number; incompleteFiles: number; invalidFiles: number; unstableFiles: number; unverifiedFiles: number; symlinksSkipped: number };
  lastBackup?: ClaudeSessionSnapshot | null;
}

export interface ClaudeSessionSnapshot {
  ok: boolean;
  backupId: string;
  backupPath: string;
  files: number;
  bytes: number;
  unstableFiles: number;
  incompleteFiles: number;
  invalidFiles: number;
  receiptPath: string;
}
