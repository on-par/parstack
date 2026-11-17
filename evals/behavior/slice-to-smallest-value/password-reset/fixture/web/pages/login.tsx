export default function LoginPage() {
  return (
    <form method="post" action="/api/login">
      <input name="email" type="email" />
      <input name="password" type="password" />
      <button type="submit">Sign in</button>
    </form>
  );
}
