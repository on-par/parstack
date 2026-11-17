import { findUser } from "@/users";

export function createInvoice(userId: string): string {
  return `invoice for ${findUser(userId)}`;
}
