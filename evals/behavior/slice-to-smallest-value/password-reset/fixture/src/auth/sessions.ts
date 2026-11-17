const sessions = new Map<string, string>();

export function createSession(userId: string): string {
  const id = Math.random().toString(36).slice(2);
  sessions.set(id, userId);
  return id;
}

export function sessionsFor(userId: string): string[] {
  return [...sessions].filter(([, u]) => u === userId).map(([id]) => id);
}
