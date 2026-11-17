export function checkPasswordRules(password: string): string[] {
  const problems: string[] = [];
  if (password.length < 12) problems.push("at least 12 characters");
  if (!/[0-9]/.test(password)) problems.push("at least one digit");
  return problems;
}
