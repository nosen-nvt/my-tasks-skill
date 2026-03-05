# bash completion for sandbox command
# Source this file or place it in /etc/bash_completion.d/

_sandbox() {
    local cur prev
    _init_completion || return

    # -- 以降はコマンド補完（デフォルトのファイル名補完に委譲）
    local i
    for ((i = 1; i < cword; i++)); do
        if [[ "${words[i]}" == "--" ]]; then
            _command_offset "$((i + 1))"
            return
        fi
    done

    case "$prev" in
        --project)
            local projects_dir="$HOME/.local/share/my-tasks/projects"
            if [[ -d "$projects_dir" ]]; then
                local ids
                ids=$(cd "$projects_dir" && ls -1 *.json 2>/dev/null | sed 's/\.json$//')
                COMPREPLY=($(compgen -W "$ids" -- "$cur"))
            fi
            return
            ;;
        --sandbox-profile)
            local profiles_dir="$HOME/.local/share/my-tasks/sandbox-profiles"
            local builtins="default unrestricted"
            local custom=""
            if [[ -d "$profiles_dir" ]]; then
                custom=$(cd "$profiles_dir" && ls -1 *.json 2>/dev/null | sed 's/\.json$//')
            fi
            COMPREPLY=($(compgen -W "$builtins $custom" -- "$cur"))
            return
            ;;
        --proxy-port)
            COMPREPLY=()
            return
            ;;
        --env-file)
            _filedir
            return
            ;;
    esac

    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--project --proxy-port --sandbox-profile --env-file --help" -- "$cur"))
        return
    fi
}

complete -F _sandbox sandbox
