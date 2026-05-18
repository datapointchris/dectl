from rich.console import Console

console = Console()


def error(message: str) -> None:
    console.print(f'[red]{message}[/red]')


def success(message: str) -> None:
    console.print(f'[green]{message}[/green]')


def info(message: str) -> None:
    console.print(message)
