import subprocess
import time
from pathlib import Path


# =========================
# НАСТРОЙКИ
# =========================

REPOSITORY_URL = "https://github.com/DAMPFERS/team-agent.git"

BRANCH = "main"

CHECK_INTERVAL = 5  # секунд

WORK_DIR = Path("/agent")


# =========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================

def run_command(command, cwd=None):
    """Выполнить команду и вернуть stdout."""
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    if result.returncode != 0:
        print("ERROR:")
        print(result.stderr)
        raise RuntimeError(f"Command failed: {command}")

    return result.stdout.strip()


def get_current_commit():
    return run_command(
        ["git", "rev-parse", "HEAD"],
        cwd=WORK_DIR,
    )


def get_remote_commit():
    return run_command(
        ["git", "rev-parse", f"origin/{BRANCH}"],
        cwd=WORK_DIR,
    )


def update_repository():
    print("Updating repository...")

    run_command(
        ["git", "fetch", "origin"],
        cwd=WORK_DIR,
    )

    run_command(
        ["git", "reset", "--hard", f"origin/{BRANCH}"],
        cwd=WORK_DIR,
    )

    print("Repository updated.")


# =========================
# GIT
# =========================

def clone_repository():

    if WORK_DIR.exists():

        if not (WORK_DIR / ".git").exists():
            raise RuntimeError(
                f"{WORK_DIR} exists but is not a git repository"
            )

        print("Repository already exists.")

        run_command(
            ["git", "fetch", "origin"],
            cwd=WORK_DIR,
        )

        return

    print("Cloning repository...")

    run_command(
        [
            "git",
            "clone",
            "--branch",
            BRANCH,
            REPOSITORY_URL,
            str(WORK_DIR),
        ]
    )

    print("Repository cloned.")


# =========================
# AGENT
# =========================

def start_agent():

    print("Starting agent...")

    return subprocess.Popen(
        ["python", "main.py"],
        cwd=WORK_DIR,
    )


def stop_agent(process):

    if process is None:
        return

    if process.poll() is None:

        print("Stopping agent...")

        process.terminate()

        try:
            process.wait(timeout=5)

        except subprocess.TimeoutExpired:

            print("Agent did not stop. Killing...")

            process.kill()
            process.wait()

        print("Agent stopped.")


# =========================
# MAIN
# =========================

def main():

    print("================================")
    print("       HACKATHON RUNNER")
    print("================================")

    clone_repository()

    current_commit = get_current_commit()

    print(f"Current commit: {current_commit}")

    process = start_agent()

    try:

        while True:

            time.sleep(CHECK_INTERVAL)

            # Проверяем, не завершился ли агент сам
            if process.poll() is not None:

                print(
                    f"Agent stopped with code "
                    f"{process.returncode}"
                )

                # Для теста автоматически запускаем снова
                process = start_agent()

                continue

            # Получаем информацию о GitHub
            run_command(
                ["git", "fetch", "origin"],
                cwd=WORK_DIR,
            )

            remote_commit = get_remote_commit()

            if remote_commit != current_commit:

                print()
                print("================================")
                print("NEW VERSION DETECTED")
                print("================================")
                print(f"Old: {current_commit}")
                print(f"New: {remote_commit}")
                print()

                # Останавливаем старую версию
                stop_agent(process)

                # Обновляем код
                update_repository()

                # Запоминаем новую версию
                current_commit = get_current_commit()

                print(
                    f"Running commit: {current_commit}"
                )

                # Запускаем новую версию
                process = start_agent()

    except KeyboardInterrupt:

        print()
        print("Runner stopping...")

        stop_agent(process)


if __name__ == "__main__":
    main()