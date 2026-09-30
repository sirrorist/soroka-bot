"""Setup script to initialize directories and check dependencies."""
# import os
from pathlib import Path


def create_directories():
    """Create necessary directories."""
    directories = [
        "temp",
        "storage",
        "logs",
    ]
    
    for directory in directories:
        Path(directory).mkdir(parents=True, exist_ok=True)
        print(f"[OK] Created directory: {directory}")


def check_env_file():
    """Check if .env file exists."""
    if not Path(".env").exists():
        print("⚠ Warning: .env file not found!")
        if Path("env_template.txt").exists():
            print("  Please copy env_template.txt to .env and fill in the values.")
        else:
            print("  Please create .env file with required configuration.")
        return False
    return True


if __name__ == "__main__":
    print("Setting up Soroka bot...")
    create_directories()
    check_env_file()
    print("\n[OK] Setup complete!")
    print("\nNext steps:")
    if Path("env_template.txt").exists():
        print("1. Copy env_template.txt to .env")
    else:
        print("1. Create .env file")
    print("2. Fill in your BOT_TOKEN and other settings")
    print("3. Run: python main.py")
