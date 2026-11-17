import { checkPasswordRules } from "./passwordRules";
import { createSession } from "./sessions";

export async function login(email: string, password: string) {
  const user = await findUserByEmail(email);
  if (!user || !(await verifyHash(password, user.passwordHash))) {
    throw new Error("invalid credentials");
  }
  return createSession(user.id);
}

declare function findUserByEmail(email: string): Promise<{ id: string; passwordHash: string } | null>;
declare function verifyHash(password: string, hash: string): Promise<boolean>;
export { checkPasswordRules };
