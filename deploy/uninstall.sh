# Sourced by ftctl with the installation lock held.
uninstall() {
    local ids id service project ref image mount volume key created answer consumers
    local state="$ROOT/.uninstall-resources" tmp
    docker info >/dev/null
    ids="$(docker ps -aq --filter "label=com.docker.compose.project.working_dir=$ROOT")"
    tmp="$(mktemp "$ROOT/.resources.XXXXXX")"
    if [ -f "$state" ]; then cat "$state" > "$tmp"; fi
    local -a containers=()
    for id in $ids; do
        service="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$id")"
        ref="$(docker inspect -f '{{.Config.Image}}' "$id")"
        case "$service:$ref" in
            app:file-transfer:*|aria2:file-transfer-aria2:*) ;;
            *) continue ;;
        esac
        [ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$id")" = "$ROOT/docker-compose.yml" ] || continue
        project="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$id")"
        image="$(docker inspect -f '{{.Image}}' "$id")"
        printf 'image %s %s\n' "$ref" "$image" >> "$tmp"
        mount="$(docker inspect -f '{{range .Mounts}}{{if eq .Type "volume"}}{{println .Name}}{{end}}{{end}}' "$id")"
        for volume in $mount; do
            key="$(docker volume inspect -f '{{index .Labels "com.docker.compose.volume"}}' "$volume")"
            case "$key" in ft-data|ft-offline|ft-aria2-state) ;; *) continue ;; esac
            [ "$volume" = "${project}_$key" ] || continue
            [ "$(docker volume inspect -f '{{index .Labels "com.docker.compose.project"}}' "$volume")" = "$project" ] || continue
            created="$(docker volume inspect -f '{{.CreatedAt}}' "$volume")"
            printf 'volume %s %s\n' "$volume" "$created" >> "$tmp"
        done
        containers+=("$id")
    done
    sort -u "$tmp" -o "$tmp"
    mv "$tmp" "$state"
    printf '本实例容器：%s\n' "${containers[*]:-无}"
    cat "$state"
    echo '默认保留数据卷。要永久删除上列本实例数据库、上传文件和离线任务，请输入 DELETE；直接回车保留。'
    IFS= read -r answer || answer=''
    for id in "${containers[@]}"; do docker stop "$id"; docker rm "$id"; done
    while read -r key ref image; do
        case "$key" in
        image)
            [ "$(docker image inspect -f '{{.Id}}' "$ref" 2>/dev/null)" = "$image" ] || continue
            consumers="$(docker ps -aq --filter "ancestor=$image")"
            if [ -z "$consumers" ]; then docker image rm "$ref"; else echo "共享镜像已保留：$ref"; fi ;;
        volume)
            [ "$answer" = DELETE ] || continue
            [ "$(docker volume inspect -f '{{.CreatedAt}}' "$ref" 2>/dev/null)" = "$image" ] || continue
            consumers="$(docker ps -aq --filter "volume=$ref")"
            if [ -z "$consumers" ]; then docker volume rm "$ref"; else echo "共享数据卷已保留：$ref"; fi ;;
        esac
    done < "$state"
    echo '卸载完成。安装目录中保留配置和重装所需文件。'
}
