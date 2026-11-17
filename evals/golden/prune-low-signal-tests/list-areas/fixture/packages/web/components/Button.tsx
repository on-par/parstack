export function Button(props: { label: string; saving?: boolean }) {
  return <button disabled={props.saving}>{props.label}</button>;
}
