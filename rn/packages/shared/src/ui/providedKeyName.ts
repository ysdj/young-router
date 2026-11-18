type RelayNameGroup = { id: string; name: string };
type RelayNameResource = {
  groupID: string;
  groupName: string;
  name: string;
  apiName: string;
};
type Translate = (key: string) => string;

function resourceGroup(resource: RelayNameResource, groups: RelayNameGroup[]): RelayNameGroup | undefined {
  return groups.find((group) => group.id === resource.groupID);
}

export function resourceGroupName(resource: RelayNameResource, groups: RelayNameGroup[], translate: Translate): string {
  const group = resourceGroup(resource, groups);
  return group?.name || resource.groupName || (resource.groupID ? resource.groupID : translate("relay.apiKeyUngrouped"));
}

/** A provided key uses its group name, or its own name when it has no group. */
export function providedKeyName(resource: RelayNameResource, groups: RelayNameGroup[], translate: Translate): string {
  const group = resourceGroup(resource, groups);
  const hasGroup = Boolean(group || resource.groupID || resource.groupName);
  if (hasGroup) {
    return resourceGroupName(resource, groups, translate);
  }
  return resource.name || resource.apiName || translate("relay.apiKeyUngrouped");
}
